"""
Process helpers: evicting whatever holds a camera, and the health log.

Eviction exists for kiosk deployments where the previous dashboard
instance (or a stray ffmpeg/motion process) still holds /dev/videoN after a
crash. It is gated by ``camera.kill_device_holders`` in config.ini and only
used by the boot-time discovery pass, never by the runtime rescan.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import signal
import subprocess
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ui.widgets import CameraWidget


def run_cmd(cmd: str, timeout: int = 2) -> tuple[str, str, int]:
    """Run a command and return stdout, stderr, returncode.

    The command string is split with shlex to avoid ``shell=True``.
    """
    try:
        result = subprocess.run(
            shlex.split(cmd), capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except Exception:
        return "", "", 1


def get_pids_from_lsof(device_path: str) -> set[int]:
    """Get PIDs holding device using lsof (preferred: one PID per line)."""
    out, _, code = run_cmd(f"lsof -t {device_path}")
    if code != 0 or not out:
        return set()
    pids: set[int] = set()
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.add(int(line))
    return pids


def get_pids_from_fuser(device_path: str) -> set[int]:
    """Get PIDs holding device using fuser (fallback when lsof is absent).

    fuser's output format varies by version and mixes PIDs with access
    letters ("1234m"), so every digit run is taken as a PID.
    """
    out, _, code = run_cmd(f"fuser -v {device_path}")
    if code != 0 or not out:
        return set()
    pids: set[int] = set()
    for match in re.findall(r"\b(\d+)\b", out):
        pids.add(int(match))
    return pids


def is_pid_alive(pid: int) -> bool:
    """Check if a PID exists."""
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def kill_device_holders(device_path: str, grace: float = 0.4) -> bool:
    """
    Attempt to terminate any process holding a camera device.
    Useful for kiosk-style setups.
    """
    from core import config
    
    if not config.KILL_DEVICE_HOLDERS:
        return False
        
    pids = get_pids_from_lsof(device_path)
    if not pids:
        pids = get_pids_from_fuser(device_path)

    # Never kill ourselves: after a settings-tile restart the new process has
    # the same PID as the one that opened the device.
    pids.discard(os.getpid())
    if not pids:
        return False

    logging.info("Killing holders of %s: %s", device_path, sorted(pids))

    # SIGTERM first, then SIGKILL after a grace period for anything that
    # ignored it. A holder owned by another user needs sudo; fuser -k covers
    # every holder at once, so one call is enough.
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except PermissionError:
            run_cmd(f"sudo fuser -k {device_path}")
            break
        except Exception:
            logging.debug("Failed to SIGTERM pid %d", pid, exc_info=True)

    time.sleep(grace)

    for pid in list(pids):
        if is_pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except PermissionError:
                run_cmd(f"sudo fuser -k {device_path}")
            except Exception:
                logging.debug("Failed to SIGKILL pid %d", pid, exc_info=True)

    return True


def log_health_summary(
    camera_widgets: list["CameraWidget"],
    placeholder_slots: list["CameraWidget"],
    active_indexes: set[int],
    failed_indexes: dict[int, float],
    stale_threshold_sec: float = 10.0,
) -> None:
    """Log a one-line health summary of all cameras.

    Reads the tiles' ``_latest_frame``, ``_last_frame_ts`` and ``worker``
    directly. That coupling is deliberate: this is the operator's view of
    the same state the tile uses, and a separate "health" API would drift.

    ``stale_threshold_sec`` is intentionally looser than the tile's own
    STALE_FRAME_TIMEOUT_SEC: the tile reacts to a 1.5 s gap, this line only
    reports gaps the tile failed to recover from.
    """
    now = time.time()
    online = 0
    stale = 0
    unhealthy_workers = 0
    
    for w in camera_widgets:
        has_frame = getattr(w, "_latest_frame", None) is not None
        last_ts = getattr(w, "_last_frame_ts", 0.0)
        worker = getattr(w, "worker", None)
        
        # Check if worker thread is healthy
        if worker is not None and hasattr(worker, "is_healthy"):
            if not worker.is_healthy():
                unhealthy_workers += 1
                cam_idx = getattr(w, "camera_stream_link", "?")
                logging.warning("Camera %s worker unhealthy (thread dead or stalled)", cam_idx)
        
        # Check frame freshness
        if has_frame:
            if last_ts > 0 and (now - last_ts) > stale_threshold_sec:
                stale += 1
                cam_idx = getattr(w, "camera_stream_link", "?")
                logging.warning(
                    "Camera %s has stale frame (%.1fs old)",
                    cam_idx,
                    now - last_ts,
                )
            else:
                online += 1
    
    logging.info(
        "Health cameras online=%d stale=%d unhealthy_workers=%d/%d placeholders=%d active=%d failed=%d",
        online,
        stale,
        unhealthy_workers,
        len(camera_widgets),
        len(placeholder_slots),
        len(active_indexes),
        len(failed_indexes),
    )
