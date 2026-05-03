# LL-Connect-Wireless (no-whiz fork)

A Linux daemon for controlling **Lian Li wireless fans** (V1 USB dongles, VID `0x0416`, PIDs `0x8040` TX / `0x8041` RX). This is a fork of [Yoinky3000/LL-Connect-Wireless](https://github.com/Yoinky3000/LL-Connect-Wireless) that fixes the audible periodic "whizzing" the upstream daemon (and every other Linux Lian-Li daemon I tried) exhibits in steady state.

## What's the problem this fixes?

If you've used the Lian-Li wireless dongle on Linux you've probably noticed: even when commanded PWM never changes, your fans periodically spike up and back down — a soft, repetitive "whizzz, whizzz" sound every 1-10 seconds. It's there with `sgtaziz/lian-li-linux` and the upstream of this fork too.

Two root causes:

1. **Missing RF keepalive.** Both Linux daemons send PWM commands to fans only when the value *changes*. Once you're at a constant target, no further packets go out. The fan firmware has a short keepalive (~100ms) — when it stops hearing from the dongle, it falls back to a hardware safe-mode (full speed) until the next packet arrives. That's the whiz: brief lapse to safe-mode, then back to commanded.
2. **RF queue overflow + audible "ack" on each packet.** Solving #1 by hammering the dongle continuously causes a different problem — every transmit packet triggers a brief audible response from the fan firmware (looks like a momentary RPM ramp). And firing transactions to multiple fans back-to-back overflows the dongle's queue, so most of the new commands are silently dropped.

Windows L-Connect 3 doesn't have either issue because it implements the proper RF keepalive protocol that Lian-Li never documented. We can't see that code, so this fork works around it instead.

## What this fork does

The architecture is split into three threads with carefully tuned cadences:

- **`discovery_thread`** (every 2s) — re-scans the dongle for the bound fan list. Long sleeps OK; doesn't block anything.
- **`temp_thread`** (every 500ms) — reads CPU temp (psutil or a custom shell command, see below) and computes the curve target.
- **`transmit_thread`** (every 20ms outer) — sends the current PWM to all fans, **dual-mode**:
  - **Steady state**: rapid back-to-back transmits to all 4 fan groups, no per-fan gap. The dongle coalesces the constant identical packets and the fans don't see "new" RF events to whir at. Result: silent at constant PWM.
  - **On change** (any commanded PWM differs from last sent by more than a hysteresis threshold): switches to a multi-cycle "delivery burst" — 4 cycles, each with a 150ms per-fan gap. This lets the dongle process each fan's transaction cleanly and gets the new value to all 4 fans in ~2.4s. Then automatically back to silent rapid mode.

Plus a few smaller pieces:

- **Hysteresis (5 PWM bytes ≈ 2%)** in the transmit thread — micro PWM changes from temp jitter are absorbed and don't trigger a delivery burst. Real curve transitions (e.g. CPU jumping from 50 → 70 °C) still ramp the fans normally.
- **Master device filtered from transmits** — the dongle reports an unbound master device with `rx_type=255`. Sending PWM frames to that confuses the dongle. Filtered out, but still kept in `/status` for visibility.
- **`cpu_temp_command` config field** — set this to a shell command (or path to a script), and the daemon will execute it every 500ms and parse stdout as the CPU temperature in °C. Useful for testing curves with fake temperatures, blending sensors, or feeding any custom temperature signal you want.

## Trade-offs

- **CPU/USB usage** is higher than upstream — the transmit thread runs continuously at ~50 Hz instead of going idle when nothing changes. On any modern desktop the load is unmeasurable, but it is non-zero.
- **A real temperature change still produces a brief audible ramp** — that's unavoidable, the fans physically need to change speed. What's eliminated is the *idle-state* periodic whiz that has nothing to do with real temperature movement.
- **Pump and AIO LCD aren't handled** — same as upstream. This daemon only does fans. If you have a HydroShift II AIO and want pump/LCD control, you'll need a separate solution.

## Install (Arch / EndeavourOS)

```bash
sudo pacman -S --needed base-devel git
git clone https://github.com/hwnprsd/LL-Connect-Wireless ~/src/LL-Connect-Wireless
cd ~/src/LL-Connect-Wireless
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
./arch/compile.sh
sudo pacman -U ./arch/.result/ll-connect-wireless-*.pkg.tar.zst
systemctl --user daemon-reload
systemctl --user enable --now ll-connect-wireless.service
loginctl enable-linger $USER     # so the daemon starts at boot, not at login
```

For Debian/Ubuntu/Fedora install paths, see the [original upstream README](UPSTREAM_README.md).

## Configuration

Settings are stored at `~/.config/ll-connect-wireless/config.json` and the CLI is the same as upstream. Quick reference:

```bash
# 4-point temperature curve
ll-connect-wireless settings set-mode curve
ll-connect-wireless settings curve set-cpu-curve "30:55,60:65,75:80,85:100"

# Or a fixed PWM
ll-connect-wireless settings set-mode linear
ll-connect-wireless settings linear set-curve 30           # constant 30%
ll-connect-wireless settings linear set-curve 35:25,80:80  # linear ramp

# Live monitor
ll-connect-wireless
```

### Custom temperature source

Drop in a script (or any shell command) that prints a number in °C:

```bash
cat > ~/.config/ll-connect-wireless/cpu_temp_test.sh <<'EOF'
#!/bin/bash
echo 50
EOF
chmod +x ~/.config/ll-connect-wireless/cpu_temp_test.sh

jq '. + {cpu_temp_command: "/home/<you>/.config/ll-connect-wireless/cpu_temp_test.sh"}' \
  ~/.config/ll-connect-wireless/config.json > /tmp/cfg.new && \
  mv /tmp/cfg.new ~/.config/ll-connect-wireless/config.json

systemctl --user restart ll-connect-wireless.service
```

The daemon picks up the new value within ~500ms. To revert to real Tctl, `jq 'del(.cpu_temp_command)'` and restart.

## Credits

- **Upstream**: [Yoinky3000/LL-Connect-Wireless](https://github.com/Yoinky3000/LL-Connect-Wireless) — the Python daemon + CLI this is based on.
- **Protocol research**: [ealcantara22/OpenUniLink](https://github.com/ealcantara22/OpenUniLink) — original reverse-engineering of the Lian-Li wireless dongle protocol.
- **Hardware reference**: Lian Li® L-Connect 3 Windows software (the reference for "what the protocol should look like when it's working right"). Lian Li and L-Connect are trademarks of Lian-Li Industrial Co., Ltd.

## Disclaimer

Not affiliated with, endorsed by, or supported by Lian-Li. This is a reverse-engineered implementation, use at your own risk. If your fans catch fire or your AIO leaks, that's between you and physics.
