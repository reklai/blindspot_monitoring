"""Process-recovery and health-reporting helpers used by the dashboard."""

from __future__ import annotations

import fcntl
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
    """Run ``cmd`` without a shell and return stripped output plus exit status.

    Shell syntax is intentionally unsupported: ``shlex.split`` converts the
    string directly to an argument vector. Startup, timeout, and other
    execution errors collapse to ``("", "", 1)`` for best-effort callers.
    """
    try:
        result = subprocess.run(
            shlex.split(cmd), capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except Exception:
        return "", "", 1


def get_pids_from_lsof(device_path: str) -> set[int]:
    """Return numeric PIDs reported by ``lsof`` for ``device_path``."""
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
    """Return numeric PID tokens reported by ``fuser`` for ``device_path``."""
    out, _, code = run_cmd(f"fuser -v {device_path}")
    if code != 0 or not out:
        return set()
    pids: set[int] = set()
    for match in re.findall(r"\b(\d+)\b", out):
        pids.add(int(match))
    return pids


def is_pid_alive(pid: int) -> bool:
    """Return whether sending signal 0 to ``pid`` succeeds."""
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def kill_device_holders(device_path: str, grace: float = 0.4) -> bool:
    """Best-effort terminate other processes holding a camera device.

    This kiosk recovery path is disabled unless ``KILL_DEVICE_HOLDERS`` is
    enabled. It tries ``lsof`` before ``fuser``, excludes this process, sends
    SIGTERM, then SIGKILL to survivors after ``grace`` seconds. A permissions
    failure falls back to ``sudo fuser -k``.

    Returns ``True`` when at least one holder was found; it does not guarantee
    that every holder exited.
    """
    from core import config
    
    if not config.KILL_DEVICE_HOLDERS:
        return False
        
    pids = get_pids_from_lsof(device_path)
    if not pids:
        pids = get_pids_from_fuser(device_path)

    pids.discard(os.getpid())
    if not pids:
        return False

    logging.info("Killing holders of %s: %s", device_path, sorted(pids))

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


def set_cloexec_on_device_fds(prefix: str = "/dev/video") -> int:
    """Mark matching open device descriptors close-on-exec.

    Camera-tile cleanup is best-effort and may leave an OpenCV descriptor open.
    The settings-tile restart uses ``os.execv``, which keeps the same PID, while
    holder cleanup deliberately excludes that PID. Marking any matching
    descriptors prevents a successful exec from carrying them into the
    replacement process.

    Marking a descriptor does not close it or interrupt current I/O; the
    kernel closes it only during a successful ``exec``. This Linux ``/proc``
    scan is best-effort and returns the number successfully marked.
    """
    count = 0
    try:
        fd_names = os.listdir("/proc/self/fd")
    except OSError:
        return 0
    for name in fd_names:
        try:
            fd = int(name)
            target = os.readlink(f"/proc/self/fd/{fd}")
        except (ValueError, OSError):
            continue
        if not target.startswith(prefix):
            continue
        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFD)
            fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
            count += 1
        except OSError:
            continue
    return count


def log_health_summary(
    camera_widgets: list["CameraWidget"],
    placeholder_slots: list["CameraWidget"],
    active_ports: set[str],
    failed_ports: dict[str, float],
    stale_threshold_sec: float = 10.0,
) -> None:
    """Log aggregate tile health and warnings for stale frames or workers.

    A retained frame counts as online unless its positive timestamp is older
    than ``stale_threshold_sec``. Worker health is counted independently
    because a stalled worker can leave its last frame displayed. The other
    collections provide slot and camera-identity bookkeeping totals for the
    summary line. Identity keys are stable USB port paths when available and
    synthetic ``index:N`` values otherwise.
    """
    now = time.time()
    online = 0
    stale = 0
    unhealthy_workers = 0
    
    for w in camera_widgets:
        has_frame = getattr(w, "_latest_frame", None) is not None
        last_ts = getattr(w, "_last_frame_ts", 0.0)
        worker = getattr(w, "worker", None)
        
        if worker is not None and hasattr(worker, "is_healthy"):
            if not worker.is_healthy():
                unhealthy_workers += 1
                cam_idx = getattr(w, "camera_stream_link", "?")
                logging.warning("Camera %s worker unhealthy (thread dead or stalled)", cam_idx)
        
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
        len(active_ports),
        len(failed_ports),
    )
