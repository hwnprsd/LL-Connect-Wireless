import os
import time
import threading
import sys
import subprocess
import usb.core
import usb.util
import psutil
import uvicorn
from fastapi import FastAPI
from parseArg import extractVersion
from utils import DEV_MODE, SOCKET_DIR, SOCKET_PATH, load_settings
from models import CurveMode, Fan, FanMode, LinearMode, SystemStatus
from typing import List, Literal, Optional
from vars import APP_NAME, APP_RAW_VERSION

shared_state: SystemStatus = None


def update_state(cpu_temp: Optional[float], gpu_temp: Optional[float], fans: List[Fan]):
    global shared_state
    shared_state = SystemStatus(
        timestamp=time.time(), cpu_temp=cpu_temp, gpu_temp=gpu_temp, fans=fans
    )


# ==============================
# SOCK SERVER
# ==============================

app = FastAPI()


@app.get("/status", response_model=SystemStatus)
async def get_status():
    return shared_state


@app.post("/reload-settings")
async def reload_settings():
    global SETTINGS, _known_fans
    SETTINGS = load_settings()
    print(f"[reload] SETTINGS reloaded: mode={SETTINGS.mode} linear={SETTINGS.linear}", flush=True)
    # Force re-discovery so the dongle's RF state is refreshed for all fans.
    # Without this, only the AIO seems to pick up new PWM; case fans need a
    # full daemon restart. Theory: dongle caches per-fan addressing and
    # re-discovery re-arms it.
    with _state_lock:
        _known_fans = []
    return {"msg": "ok"}


@app.get("/")
async def root():
    return {"status": "running", "service": APP_NAME}


def start_api_server():
    os.makedirs(SOCKET_DIR, exist_ok=True)
    uvicorn.run(app, uds=SOCKET_PATH, log_level="warning")


# ==============================
# USB CONSTANTS
# ==============================
VID = 0x0416
TX = 0x8040
RX = 0x8041

USB_OUT = 0x01
USB_IN = 0x81

GET_DEV_CMD = 0x10
RF_PAGE_STRIDE = 434
MAX_DEVICES_PAGE = 10

# ==============================
# USER CONFIG
# ==============================
# MIN_PWM = 20
# MAX_PWM = 175

# MIN_TEMP = 35.0
# MAX_TEMP = 85.0
SETTINGS = load_settings()

LOOP_INTERVAL = 0.01
TX_INTERVAL = 0.02       # transmit thread cadence (s) — fast outer loop, fan-burst inner
DISCOVERY_INTERVAL = 2.0 # how often to re-discover the fan list (s)
TEMP_INTERVAL = 0.5      # how often to re-read temp + recompute curve (s)

# ==============================
# SHARED STATE (for the 3-thread design)
# ==============================
_state_lock = threading.Lock()
_known_fans: List["Fan"] = []
_target_pwm_cpu: Optional[int] = None
_target_pwm_gpu: Optional[int] = None
_last_cpu_temp: Optional[float] = None
_last_gpu_temp: Optional[float] = None
_resync_requested: bool = False  # set by discovery_thread when it sees a fan whose actual PWM has drifted from target; cleared by transmit_thread after firing a burst


# ==============================
# UTILS
# ==============================
def u8(x):
    return bytes([x & 0xFF])


def mac_to_bytes(mac):
    return bytes(int(b, 16) for b in mac.split(":"))


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def clear_console():
    sys.stdout.write("\033[H\033[J")
    sys.stdout.flush()


def displayDetected(fans: List[Fan]):
    print("Detected devices:\n")
    print(f"{'MAC Address':17}  Fans  Channel  RX  Bound")
    print("-" * 50)
    for f in fans:
        print(
            f"{f.mac:17}  "
            f"{f.fan_count:>4}     "
            f"{f.channel:>3}     "
            f"{f.rx_type:>2}   "
            f"{'yes' if f.is_bound else 'no'}"
        )


# ==============================
# USB DEVICE HANDLING
# ==============================
def open_device(pid: Literal[32832]):
    dev = usb.core.find(idVendor=VID, idProduct=pid)
    if dev is None:
        raise RuntimeError(f"Device {pid:04x} not found")
    if dev.is_kernel_driver_active(0):
        try:
            dev.detach_kernel_driver(0)
        except usb.core.USBError as e:
            print(f"Could not detach kernel driver: {e}")
    usb.util.claim_interface(dev, 0)
    return dev


def fetch_page(rx: usb.core.Device, page_count: int):
    cmd = bytearray(64)
    cmd[0] = GET_DEV_CMD
    cmd[1] = page_count & 0xFF

    rx.write(USB_OUT, cmd)

    total_len = RF_PAGE_STRIDE * page_count
    buf = bytearray()

    while len(buf) < total_len:
        try:
            chunk = rx.read(USB_IN, 512, timeout=500)
        except usb.core.USBError as e:
            print(e)
            return bytearray()

        buf.extend(chunk)
        if len(chunk) < 512:
            break

    return buf


def list_fans(rx: usb.core.Device, last_fans_data: List[Fan] = []):
    payload = fetch_page(rx, 1)
    if not payload or payload is None or payload == b"":
        return []
    count = payload[1]
    fans: List[Fan] = []
    offset = 4

    for _ in range(count):
        record = payload[offset : offset + 42]
        offset += 42

        if record[41] != 28:
            continue

        mac = ":".join(f"{b:02x}" for b in record[0:6])
        previous_target_pwm = next((d for d in last_fans_data if d.mac == mac), 0)
        fans.append(
            Fan(
                mac=mac,
                master_mac=":".join(f"{b:02x}" for b in record[6:12]),
                channel=record[12],
                rx_type=record[13],
                fan_count=record[19] % 10,
                pwm=list(record[36:40])[0],
                rpm=[
                    (record[28] << 8) | record[29],
                    (record[30] << 8) | record[31],
                    (record[32] << 8) | record[33],
                    (record[34] << 8) | record[35],
                ],
                target_pwm=previous_target_pwm if not previous_target_pwm else previous_target_pwm.target_pwm,
                is_bound=record[6:12] != b"\x00" * 6,
            )
        )

    return fans


# ==============================
# CPU/GPU TEMP
# ==============================
def get_cpu_temp():
    cmd = getattr(SETTINGS, "cpu_temp_command", None)
    if cmd:
        try:
            output = subprocess.check_output(
                ["/bin/sh", "-c", cmd],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.0,
            ).strip()
            val = float(output.splitlines()[0])
            return val
        except Exception as e:
            print(f"[cpu_temp_command] failed: {e}", flush=True)
            return None
    # No custom command set — log this once-ish so we can confirm
    if not getattr(get_cpu_temp, "_logged_no_cmd", False):
        print(f"[cpu_temp] no cpu_temp_command in SETTINGS, using psutil. SETTINGS keys: {list(SETTINGS.model_dump().keys())}", flush=True)
        get_cpu_temp._logged_no_cmd = True

    temps = psutil.sensors_temperatures()
    tctl = None
    values = []

    for _, entries in temps.items():
        for e in entries:
            if e.current is not None:
                if e.label == "Tctl":
                    tctl = e.current
                values.append(e.current)

    return tctl if tctl else (max(values) if values else None)


def get_gpu_temp():
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return None

    values: List[float] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            values.append(float(line))
        except ValueError:
            continue
    return max(values) if values else None


# ==============================
# TEMP → PWM
# ==============================
def temp_to_pwm(temp: float, linear: LinearMode):
    t = clamp(temp, linear.min_temp, linear.max_temp)

    delta = linear.max_temp - linear.min_temp
    if delta <= 0:
        return int(linear.min_pwm / 100 * 255)

    ratio = (t - linear.min_temp) / delta

    pwm_percent = linear.min_pwm + ratio * (linear.max_pwm - linear.min_pwm)

    pwm_percent = clamp(pwm_percent, 0, 100)

    return int(round(pwm_percent / 100 * 255))


def curve_to_pwm(temp: float, curve: CurveMode):
    points = curve.points

    if temp <= points[0].temp_c:
        return int(round(points[0].percent / 100 * 255))
    if temp >= points[-1].temp_c:
        return int(round(points[-1].percent / 100 * 255))

    for i in range(1, len(points)):
        left = points[i - 1]
        right = points[i]
        if temp <= right.temp_c:
            ratio = (temp - left.temp_c) / (right.temp_c - left.temp_c)
            pwm_percent = left.percent + ratio * (right.percent - left.percent)
            pwm_percent = clamp(pwm_percent, 0, 100)
            return int(round(pwm_percent / 100 * 255))

    return int(round(points[-1].percent / 100 * 255))


# ==============================
# BUILD USB DATA
# ==============================
def build_data(fan: Fan, seq):
    frame = bytearray()
    frame += u8(0x10)
    frame += u8(seq)
    frame += u8(fan.channel)
    frame += u8(fan.rx_type)
    frame += u8(0x12)
    frame += u8(0x10)

    if seq == 0:
        frame += mac_to_bytes(fan.mac)
        frame += mac_to_bytes(fan.master_mac)
        frame += u8(fan.rx_type)
        frame += u8(fan.channel)
        frame += u8(fan.rx_type)
        frame += bytes([fan.pwm] * 4)
    else:
        frame += bytes(6)
        frame += bytes(6)
        frame += bytes(3)
        frame += bytes(4)
    return frame


def build_multi_data(fans, seq):
    """Multi-fan packet: each seq slot encodes one fan's data so a single
    transaction targets all fans simultaneously."""
    frame = bytearray()
    frame += u8(0x10)
    frame += u8(seq)
    if seq < len(fans):
        fan = fans[seq]
        frame += u8(fan.channel)
        frame += u8(fan.rx_type)
        frame += u8(0x12)
        frame += u8(0x10)
        frame += mac_to_bytes(fan.mac)
        frame += mac_to_bytes(fan.master_mac)
        frame += u8(fan.rx_type)
        frame += u8(fan.channel)
        frame += u8(fan.rx_type)
        frame += bytes([fan.pwm] * 4)
    else:
        frame += bytes(4)
        frame += bytes(6)
        frame += bytes(6)
        frame += bytes(3)
        frame += bytes(4)
    return frame


# ==============================
# 3-THREAD DESIGN
# ==============================
# Original monolithic loop called list_fans() (USB read, up to 500ms timeout)
# every iteration, blocking transmits. Now split into independent threads:
#   - discovery_thread: re-discovers fan list every DISCOVERY_INTERVAL
#   - temp_thread:      reads CPU/GPU temp + computes target PWM every TEMP_INTERVAL
#   - transmit_thread:  sends PWM to known fans every TX_INTERVAL (never blocks on reads)


def _compute_targets(cpu_temp: Optional[float], gpu_temp: Optional[float],
                     should_read_gpu_temp: bool):
    cpu_target_pwm = None
    gpu_target_pwm = None
    if SETTINGS.mode == FanMode.linear:
        if cpu_temp is not None:
            cpu_target_pwm = temp_to_pwm(cpu_temp, SETTINGS.linear)
        if should_read_gpu_temp and gpu_temp is not None:
            gpu_target_pwm = temp_to_pwm(gpu_temp, SETTINGS.gpu_linear)
        elif should_read_gpu_temp and cpu_temp is not None:
            gpu_target_pwm = temp_to_pwm(cpu_temp, SETTINGS.gpu_linear)
    else:
        if cpu_temp is not None:
            cpu_target_pwm = curve_to_pwm(cpu_temp, SETTINGS.cpu_curve)
        if should_read_gpu_temp and gpu_temp is not None:
            gpu_target_pwm = curve_to_pwm(gpu_temp, SETTINGS.gpu_curve)
        elif should_read_gpu_temp and cpu_temp is not None:
            gpu_target_pwm = curve_to_pwm(cpu_temp, SETTINGS.cpu_curve)
    return cpu_target_pwm, gpu_target_pwm


def discovery_thread(rx: usb.core.Device):
    global _known_fans, _resync_requested
    # When a fan's actual PWM (read back from the dongle) drifts from our
    # commanded target by more than this many bytes, request a resync burst
    # so the rebroadcast reaches any fans that missed earlier transmits.
    # Set LLCW_DIVERGE_THRESHOLD=999 to disable monitoring.
    DIVERGENCE_THRESHOLD = int(os.environ.get("LLCW_DIVERGE_THRESHOLD", "8"))
    iteration = 0
    while True:
        try:
            with _state_lock:
                snap = list(_known_fans)
                cpu_target = _target_pwm_cpu
                gpu_target = _target_pwm_gpu
                gpu_macs = set(SETTINGS.gpu_temp_macs)
            new_fans = list_fans(rx, snap)
            iteration += 1
            if iteration % 5 == 0:
                print(f"[discovery] iter={iteration} got {len(new_fans)} fan(s)", flush=True)
            if new_fans:
                # Divergence check: new_fans[i].pwm here is the actual PWM the
                # fan firmware reports, straight from the dongle (record[36:40]
                # in list_fans). Compare to commanded target.
                diverged_macs = []
                for f in new_fans:
                    if not f.is_bound or f.rx_type == 255:
                        continue
                    target = gpu_target if f.mac.lower() in gpu_macs else cpu_target
                    if target is None:
                        continue
                    if abs(f.pwm - target) > DIVERGENCE_THRESHOLD:
                        diverged_macs.append((f.mac, f.pwm, target))

                with _state_lock:
                    prev_targets = {f.mac: f.target_pwm for f in _known_fans}
                    for f in new_fans:
                        if f.mac in prev_targets and prev_targets[f.mac]:
                            f.target_pwm = prev_targets[f.mac]
                            # Note: do NOT clobber f.pwm here — preserve the
                            # actual reading for visibility and future checks.
                    _known_fans = new_fans
                    if diverged_macs:
                        _resync_requested = True

                if diverged_macs:
                    for mac, actual, target in diverged_macs:
                        print(f"[discovery] DIVERGE mac={mac} actual={actual} target={target} -> requesting resync", flush=True)
        except Exception as e:
            print(f"[discovery] ERROR iter={iteration}: {type(e).__name__}: {e}", flush=True)
        time.sleep(DISCOVERY_INTERVAL)


def temp_thread():
    global _target_pwm_cpu, _target_pwm_gpu, _last_cpu_temp, _last_gpu_temp
    last_cpu_pwm = None
    while True:
        try:
            cpu_temp = get_cpu_temp()
            gpu_mac_set = set(SETTINGS.gpu_temp_macs)
            should_read_gpu_temp = len(gpu_mac_set) > 0
            gpu_temp = get_gpu_temp() if should_read_gpu_temp else None
            cpu_pwm, gpu_pwm = _compute_targets(cpu_temp, gpu_temp, should_read_gpu_temp)
            if cpu_pwm != last_cpu_pwm:
                print(f"[temp] cpu_pwm changed {last_cpu_pwm} -> {cpu_pwm} (temp={cpu_temp}, mode={SETTINGS.mode})", flush=True)
                last_cpu_pwm = cpu_pwm
            with _state_lock:
                _target_pwm_cpu = cpu_pwm
                _target_pwm_gpu = gpu_pwm
                _last_cpu_temp = cpu_temp
                _last_gpu_temp = gpu_temp
        except Exception as e:
            print(f"[temp] error: {type(e).__name__}: {e}", flush=True)
        time.sleep(TEMP_INTERVAL)


def reclaim_tx_device(old_tx: usb.core.Device) -> usb.core.Device:
    """Release the TX USB handle and re-acquire it.

    This is the programmatic equivalent of a `systemctl restart` for just the
    USB layer — the dongle sees the bulk endpoint go away, drains its queue,
    and starts fresh. After reclaim, the next transmit lands cleanly instead
    of being coalesced into a saturated rapid-mode queue. Required because
    fans that have tripped hardware safe-mode (full speed) only exit when a
    fresh PWM packet actually arrives at their RF — and an in-flight burst
    fired into a flooded queue often doesn't make it out as a discrete frame.
    """
    try:
        usb.util.release_interface(old_tx, 0)
    except Exception as e:
        print(f"[tx] release_interface error (continuing): {e}", flush=True)
    try:
        usb.util.dispose_resources(old_tx)
    except Exception as e:
        print(f"[tx] dispose_resources error (continuing): {e}", flush=True)
    # Brief pause so the dongle's queue actually drains and the USB stack
    # settles before we re-acquire. Too short = no benefit; too long = fans
    # may trip safe-mode while we're disconnected.
    time.sleep(0.4)
    new_tx = open_device(0x8040)
    return new_tx


def transmit_thread(tx: usb.core.Device):
    # Rapid back-to-back transmits to all fans every cycle, no per-fan gap.
    # When a real value change is detected, switch to a multi-cycle "delivery
    # burst" with per-fan gap so all 4 fans actually receive the new value
    # (single-burst isn't enough to overcome dongle queue saturation).
    # Default 0 = no hysteresis (every PWM delta triggers a delivery burst).
    # To revert to the old jitter-suppressing behaviour, set the env var
    # LLCW_HYSTERESIS=5 (or any byte count) in the systemd unit — no rebuild.
    global _resync_requested
    HYSTERESIS = int(os.environ.get("LLCW_HYSTERESIS", "0"))
    BURST_CYCLES_ON_CHANGE = 4   # # of 150ms-gap cycles after a change
    # Periodic forced re-broadcast: every N seconds, fire a delivery burst even
    # if nothing changed and no divergence was detected. This recovers from
    # fan-level RF delivery loss that the dongle-side divergence detector is
    # blind to (the dongle thinks it sent the packet; the fan never got it).
    # 0 disables periodic re-broadcasting (then only divergence-detected
    # resyncs fire). Higher = quieter but slower convergence after RF drops.
    PERIODIC_RESYNC_SEC = int(os.environ.get("LLCW_PERIODIC_RESYNC_SEC", "30"))
    last_sent_cpu = None
    last_sent_gpu = None
    remaining_burst = 0
    last_periodic_resync = time.monotonic()
    while True:
        try:
            now = time.monotonic()
            periodic_due = (
                PERIODIC_RESYNC_SEC > 0
                and (now - last_periodic_resync) >= PERIODIC_RESYNC_SEC
            )
            with _state_lock:
                fans_snap = list(_known_fans)
                cpu_pwm = _target_pwm_cpu
                gpu_pwm = _target_pwm_gpu
                cpu_temp = _last_cpu_temp
                gpu_temp = _last_gpu_temp
                gpu_mac_set = set(SETTINGS.gpu_temp_macs)
                resync_now = _resync_requested
                if resync_now:
                    _resync_requested = False

            # Apply hysteresis: small deltas snap back to the last-sent value
            if cpu_pwm is not None and last_sent_cpu is not None:
                if abs(cpu_pwm - last_sent_cpu) <= HYSTERESIS:
                    cpu_pwm = last_sent_cpu
            if gpu_pwm is not None and last_sent_gpu is not None:
                if abs(gpu_pwm - last_sent_gpu) <= HYSTERESIS:
                    gpu_pwm = last_sent_gpu

            real_fans = [f for f in fans_snap if f.is_bound]

            if (cpu_pwm is None and gpu_pwm is None) or not real_fans:
                time.sleep(TX_INTERVAL)
                continue

            # Detect a real value change (after hysteresis). On change, queue
            # up BURST_CYCLES_ON_CHANGE delivery cycles with per-fan 150ms gap
            # so all fans actually receive the new value (one cycle isn't
            # enough — the dongle queue is saturated from rapid mode and only
            # catches ~1 fan per cycle).
            cpu_changed = cpu_pwm is not None and cpu_pwm != last_sent_cpu
            gpu_changed = gpu_pwm is not None and gpu_pwm != last_sent_gpu

            # Periodic forced re-broadcast: do a real TX device reclaim, not
            # just a burst-into-saturated-queue. A pure burst from the running
            # daemon gets coalesced with rapid-mode packets and often doesn't
            # land cleanly enough to pull a fan out of hardware safe-mode.
            # Reclaim mimics `systemctl restart` for the USB layer.
            if periodic_due:
                print(f"[transmit] periodic resync -> reclaiming TX device (every {PERIODIC_RESYNC_SEC}s)", flush=True)
                try:
                    tx = reclaim_tx_device(tx)
                except Exception as e:
                    print(f"[transmit] reclaim FAILED: {type(e).__name__}: {e}", flush=True)
                last_periodic_resync = now
                # After reclaim, force a burst on the next iteration by
                # invalidating last_sent — the change-detection path will
                # naturally fire a paced delivery burst.
                last_sent_cpu = None
                last_sent_gpu = None
                # Skip remainder of this cycle so the burst happens fresh
                time.sleep(TX_INTERVAL)
                continue

            if cpu_changed or gpu_changed or resync_now:
                if resync_now and not (cpu_changed or gpu_changed):
                    print(f"[transmit] resync requested -> firing delivery burst", flush=True)
                remaining_burst = BURST_CYCLES_ON_CHANGE

            delivery_burst = remaining_burst > 0
            if delivery_burst:
                remaining_burst -= 1

            # Inner packet count must match total discovered devices (incl. master)
            packet_count = len(fans_snap)
            for fan in real_fans:
                mac = fan.mac.lower()
                if mac in gpu_mac_set and gpu_pwm is not None:
                    fan.pwm = gpu_pwm
                    fan.target_pwm = gpu_pwm
                elif cpu_pwm is not None:
                    fan.pwm = cpu_pwm
                    fan.target_pwm = cpu_pwm
                else:
                    continue
                for i in range(packet_count):
                    try:
                        tx.write(USB_OUT, build_data(fan, i))
                    except Exception:
                        pass
                if delivery_burst:
                    # Per-fan gap only on real change, so dongle has time to
                    # process each fan's transaction before the next is sent.
                    time.sleep(0.15)

            # Record what we actually sent, for hysteresis next iteration
            if cpu_pwm is not None:
                last_sent_cpu = cpu_pwm
            if gpu_pwm is not None:
                last_sent_gpu = gpu_pwm

            update_state(cpu_temp, gpu_temp, fans_snap)
        except Exception as e:
            if DEV_MODE:
                print(f"[tx] error: {e}")
        time.sleep(TX_INTERVAL)


def fan_control_loop(rx: usb.core.Device, tx: usb.core.Device):
    global _known_fans
    initial = list_fans(rx, [])
    with _state_lock:
        _known_fans = initial

    threading.Thread(target=discovery_thread, args=(rx,), daemon=True, name="discovery").start()
    threading.Thread(target=temp_thread, daemon=True, name="temp").start()
    threading.Thread(target=transmit_thread, args=(tx,), daemon=True, name="tx").start()

    while True:
        time.sleep(60)


# ==============================
# ENTRY
# ==============================
if __name__ == "__main__":
    tx = None
    rx = None
    try:
        current_ver = extractVersion(APP_RAW_VERSION)
        print(f"Current Version: {APP_RAW_VERSION}")
        print(f"- SEMVER: {current_ver.semver}")
        print(f"- Release Candidate: {current_ver.rc}")
        print(f"- Build Release: {current_ver.release}")
        print(f"Start sock server at {SOCKET_PATH}")
        api_thread = threading.Thread(target=start_api_server, daemon=True)
        api_thread.start()

        retries = 0
        while not os.path.exists(SOCKET_PATH) and retries < 50:
            time.sleep(0.2)
            retries += 1

        if os.path.exists(SOCKET_PATH):
            try:
                os.chmod(SOCKET_PATH, 0o666)
            except OSError:
                pass
        
        try:
            tx = open_device(TX)
            rx = open_device(RX)
        except Exception as e:
            print("Unable to open lian li wireless controller")
            print(e)
            sys.exit(1)

        fans = list_fans(rx, [])
        displayDetected(fans)

        time.sleep(5 if DEV_MODE else 0)

        fan_control_loop(rx, tx)

    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error: {e}")
    finally:
        if tx:
            usb.util.dispose_resources(tx)
        if rx:
            usb.util.dispose_resources(rx)

        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        sys.exit(0)
