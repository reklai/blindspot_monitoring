"""
Camera capture and discovery for Camera Dashboard.

Contains CaptureWorker for threaded video capture and
functions for discovering available cameras.
"""

from __future__ import annotations

import glob as glob_module
import logging
import os
import platform
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Optional, Union

import cv2
import numpy as np
from numpy.typing import NDArray
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from core import config
from utils import kill_device_holders


# Cache for GStreamer availability check
_gstreamer_available: Optional[bool] = None


def _check_gstreamer_available() -> bool:
    """Check if OpenCV was built with GStreamer support.
    
    Caches the result to avoid repeated checks.
    """
    global _gstreamer_available
    if _gstreamer_available is not None:
        return _gstreamer_available
    
    try:
        # Check if OpenCV has GStreamer backend support
        build_info = cv2.getBuildInformation()
        gstreamer_line = None
        for line in build_info.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("gstreamer"):
                gstreamer_line = stripped
                break
        if gstreamer_line is None:
            _gstreamer_available = False
        else:
            tokens = gstreamer_line.split()
            last_token = tokens[-1].upper() if tokens else ""
            _gstreamer_available = last_token == "YES"
        if _gstreamer_available:
            logging.info("GStreamer support detected in OpenCV build")
        else:
            logging.info("GStreamer support not available in OpenCV build")
    except Exception:
        _gstreamer_available = False
        logging.debug("Could not check GStreamer availability", exc_info=True)
    
    return _gstreamer_available


def _build_gstreamer_pipeline(
    device: Union[int, str], width: int, height: int
) -> Optional[str]:
    """Build the GStreamer v4l2src pipeline string for `device`.

    `device` is either:
      - int: a /dev/videoN index -- produces exactly today's pipeline
        string (`device=/dev/video{N}`).
      - str starting with `/dev/`: an already-resolved device path --
        produces `device={device}`.
      - any other str (e.g. an `rtsp://...` URL or a relative path):
        returns None. This GStreamer pipeline is V4L2-only; the caller
        must skip it and fall through to the V4L2/software cascade
        rather than feed an arbitrary string into v4l2src.
    """
    if isinstance(device, int):
        device_arg = f"/dev/video{device}"
    elif isinstance(device, str) and device.startswith("/dev/"):
        device_arg = device
    else:
        return None

    # Use jpegdec (libjpeg) for MJPEG decoding - stable and efficient
    # GStreamer pipeline optimized for low-latency:
    # - v4l2src: capture from V4L2 device
    # - queue: decouple source from decode (max 2 buffers, leaky=downstream)
    # - appsink: sync=false for no A/V sync overhead, drop=1 for frame dropping
    # - max-buffers=1: only keep latest frame to minimize latency
    return (
        f"v4l2src device={device_arg} ! "
        f"image/jpeg,width={width},height={height} ! "
        f"queue max-size-buffers=2 leaky=downstream ! "
        f"jpegdec ! videoconvert ! "
        f"appsink drop=1 max-buffers=1 sync=false"
    )


class CaptureWorker(QThread):
    """Background thread for capturing frames from a camera."""
    
    # Signal emitted when a new frame is ready for the UI thread.
    frame_ready = pyqtSignal(object)
    # Signal emitted when camera connection status changes.
    status_changed = pyqtSignal(bool)

    # Pre-allocated frame pool size (reduces GC pressure)
    FRAME_POOL_SIZE = 3

    def __init__(
        self,
        stream_link: Union[int, str],
        parent: Optional[QObject] = None,
        target_fps: Optional[float] = None,
        capture_width: Optional[int] = None,
        capture_height: Optional[int] = None,
    ) -> None:
        """Initialize camera capture settings and state."""
        super().__init__(parent)
        self.stream_link = stream_link
        self._running = True
        self._reconnect_backoff = 1.0
        self._cap: Optional[cv2.VideoCapture] = None
        self._last_emit = 0.0
        self._target_fps = target_fps
        self._emit_interval = 1.0 / 30.0
        self.capture_width = capture_width
        self.capture_height = capture_height
        self._online = False
        self._start_ts = time.time()
        self._open_fail_count = 0
        # Track if using GStreamer backend for proper cleanup
        self._using_gstreamer = False
        # Cached FOURCC string, updated by worker thread, read by main thread.
        self._fourcc: str = "unknown"
        # Lock protects changes to FPS/emit interval from other threads.
        self._fps_lock = threading.Lock()
        self._stop_event = threading.Event()
        
        # Pre-allocated frame pool to reduce memory allocations/GC pressure
        self._frame_pool: deque[NDArray[np.uint8]] = deque(maxlen=self.FRAME_POOL_SIZE)
        self._frame_pool_lock = threading.Lock()
        self._pool_frame_shape: Optional[tuple[int, ...]] = None

    def _get_pooled_frame(self, shape: tuple[int, ...], dtype: np.dtype) -> NDArray[np.uint8]:
        """Get a pre-allocated frame from pool or create new one.
        
        This reduces memory allocation overhead and GC pressure by reusing
        frame buffers instead of allocating new ones for each capture.
        """
        with self._frame_pool_lock:
            # If shape changed, invalidate pool
            if self._pool_frame_shape != shape:
                self._frame_pool.clear()
                self._pool_frame_shape = shape
            
            # Try to get existing frame from pool
            if self._frame_pool:
                return self._frame_pool.popleft()
        
        # Allocate new frame (contiguous for efficient Qt conversion)
        return np.empty(shape, dtype=dtype, order='C')
    
    def _return_to_pool(self, frame: NDArray[np.uint8]) -> None:
        """Return a frame buffer to the pool for reuse."""
        with self._frame_pool_lock:
            if (
                self._pool_frame_shape is not None
                and frame.shape == self._pool_frame_shape
                and len(self._frame_pool) < self.FRAME_POOL_SIZE
            ):
                self._frame_pool.append(frame)

    def return_frame(self, frame: NDArray[np.uint8]) -> None:
        """Public helper to return a frame buffer to the pool."""
        self._return_to_pool(frame)

    def run(self) -> None:
        """Capture loop: open camera, grab frames, emit, reconnect on failure."""
        self._start_ts = time.time()
        self._stop_event.clear()
        logging.info("Camera %s thread started", self.stream_link)
        while self._running:
            try:
                # Ensure capture is open; reconnect if it fails.
                if self._cap is None or not self._cap.isOpened():
                    self._open_capture()
                    if not (self._cap and self._cap.isOpened()):
                        self._open_fail_count += 1
                        if self._open_fail_count % 10 == 0:
                            logging.warning(
                                "Camera %s open failed (%d attempts)",
                                self.stream_link,
                                self._open_fail_count,
                            )
                        if self._online:
                            self._online = False
                            self.status_changed.emit(False)
                        self._stop_event.wait(timeout=self._reconnect_backoff)
                        self._reconnect_backoff = min(
                            self._reconnect_backoff * 1.5, 10.0
                        )
                        continue
                    self._reconnect_backoff = 1.0
                    self._open_fail_count = 0
                    if not self._online:
                        self._online = True
                        self.status_changed.emit(True)

                # Grab & retrieve keeps latency low vs read().
                grabbed = self._cap.grab()
                if not grabbed:
                    logging.debug(
                        "Camera %s: grab() failed, closing capture",
                        self.stream_link,
                    )
                    self._close_capture()
                    if self._online:
                        self._online = False
                        self.status_changed.emit(False)
                    continue

                ret, frame = self._cap.retrieve()
                if not ret or frame is None:
                    logging.debug(
                        "Camera %s: retrieve() failed, closing capture",
                        self.stream_link,
                    )
                    self._close_capture()
                    if self._online:
                        self._online = False
                        self.status_changed.emit(False)
                    continue

                now = time.time()
                with self._fps_lock:
                    emit_interval = self._emit_interval
                # Throttle emits to target FPS to avoid UI overload.
                if now - self._last_emit >= emit_interval:
                    # Use pooled frame to reduce allocations
                    pooled = self._get_pooled_frame(frame.shape, frame.dtype)
                    np.copyto(pooled, frame)
                    self.frame_ready.emit(pooled)
                    self._last_emit = now

                self.msleep(1)
            except Exception:
                logging.exception("Exception in CaptureWorker %s", self.stream_link)
                time.sleep(0.2)

        if self._online:
            self._online = False
            self.status_changed.emit(False)

        self._close_capture()
        logging.info("Camera %s thread stopped", self.stream_link)

    def _resolve_stream_target(self) -> Union[int, str]:
        """Resolve `stream_link` to the value actually passed to
        cv2.VideoCapture for this open attempt.

        int targets pass through unchanged (no behavior change). str
        targets are realpath'd on EVERY call -- deliberately not cached.
        This is the fast-recovery mechanism: a worker holding a
        /dev/v4l/by-path/... symlink re-resolves it on each reconnect
        attempt, so once udev re-points the symlink at a re-enumerated
        camera after a replug, the existing reconnect loop picks up the
        new /dev/videoN node within one backoff interval. If realpath
        fails for any reason, the original string is returned unchanged
        so the subsequent open attempt fails and the reconnect loop
        retries.
        """
        if isinstance(self.stream_link, int):
            return self.stream_link
        try:
            return os.path.realpath(self.stream_link)
        except Exception:
            return self.stream_link

    def _open_capture(self) -> None:
        """Open the camera and apply preferred capture settings."""
        try:
            cap = None
            backend_name = "V4L2"
            resolved_target = self._resolve_stream_target()

            def _try_v4l2_open(forced_fourcc: Optional[str]) -> Optional[cv2.VideoCapture]:
                backend = cv2.CAP_ANY
                if platform.system() == "Linux":
                    backend = cv2.CAP_V4L2
                local_cap = cv2.VideoCapture(resolved_target, backend)
                if not local_cap or not local_cap.isOpened():
                    try:
                        local_cap.release()
                    except Exception:
                        pass
                    return None
                if forced_fourcc:
                    try:
                        local_cap.set(
                            cv2.CAP_PROP_FOURCC,
                            cv2.VideoWriter_fourcc(
                                forced_fourcc[0],
                                forced_fourcc[1],
                                forced_fourcc[2],
                                forced_fourcc[3],
                            ),
                        )
                    except Exception:
                        pass
                if self.capture_width:
                    local_cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self.capture_width))
                if self.capture_height:
                    local_cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.capture_height))
                try:
                    local_cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception:
                    pass
                try:
                    local_cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 2000)
                    local_cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2000)
                except Exception:
                    pass
                try:
                    if self._target_fps and self._target_fps > 0:
                        local_cap.set(cv2.CAP_PROP_FPS, float(self._target_fps))
                    else:
                        local_cap.set(cv2.CAP_PROP_FPS, 0)
                except Exception:
                    pass
                if not local_cap.grab():
                    try:
                        local_cap.release()
                    except Exception:
                        pass
                    return None
                return local_cap

            # Try GStreamer first if enabled and available (more efficient MJPEG pipeline).
            # _build_gstreamer_pipeline returns None for stream targets it can't
            # build a V4L2 pipeline for (e.g. non-/dev/ strings), in which case
            # we skip straight to the V4L2/fallback cascade below.
            w = int(self.capture_width) if self.capture_width else 640
            h = int(self.capture_height) if self.capture_height else 480
            pipeline = (
                _build_gstreamer_pipeline(resolved_target, w, h)
                if (
                    config.USE_GSTREAMER
                    and _check_gstreamer_available()
                    and platform.system() == "Linux"
                )
                else None
            )
            if pipeline is not None:
                try:
                    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
                    if cap and cap.isOpened():
                        # Test if we can actually grab a frame
                        test_ret = cap.grab()
                        if test_ret:
                            backend_name = "GStreamer"
                            logging.info(
                                "GStreamer pipeline opened for camera %s (jpegdec)",
                                self.stream_link,
                            )
                        else:
                            cap.release()
                            cap = None
                    else:
                        if cap is not None:
                            cap.release()
                        cap = None
                except Exception as e:
                    logging.warning(
                        "GStreamer failed for camera %s: %s", self.stream_link, e
                    )
                    cap = None

            # Fallback to V4L2 if GStreamer failed or not enabled/available
            if cap is None:
                if config.USE_GSTREAMER and _check_gstreamer_available():
                    logging.info(
                        "Camera %s: GStreamer unavailable, falling back to V4L2",
                        self.stream_link,
                    )
                logging.info("Camera %s: trying V4L2 MJPG", self.stream_link)
                cap = _try_v4l2_open("MJPG")
                if cap is None:
                    logging.info("Camera %s: trying V4L2 YUYV", self.stream_link)
                    cap = _try_v4l2_open("YUYV")
                if cap is None:
                    logging.info("Camera %s: trying V4L2 auto", self.stream_link)
                    cap = _try_v4l2_open(None)
                backend_name = "V4L2"

            if not cap or not cap.isOpened():
                logging.warning(
                    "Camera %s: Failed to open capture (no backend worked)",
                    self.stream_link,
                )
                try:
                    if cap is not None:
                        cap.release()
                except Exception:
                    pass
                return

            if cap.isOpened():
                self._cap = cap
                self._using_gstreamer = backend_name == "GStreamer"
                self._configure_fps_from_camera()
                try:
                    raw = int(cap.get(cv2.CAP_PROP_FOURCC))
                    fourcc = "".join([chr((raw >> (8 * i)) & 0xFF) for i in range(4)])
                    self._fourcc = fourcc
                    if fourcc.strip() and fourcc != "MJPG":
                        logging.info(
                            "Camera %s using FOURCC=%s", self.stream_link, fourcc
                        )
                except Exception:
                    pass
                try:
                    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    actual_fps = float(cap.get(cv2.CAP_PROP_FPS))
                    logging.info(
                        "Camera %s format %dx%d @ %.1f FPS (%s)",
                        self.stream_link,
                        actual_w,
                        actual_h,
                        actual_fps,
                        backend_name,
                    )
                except Exception:
                    pass
                logging.info(
                    "Opened capture %s (requested %sx%s) -> emit fps=%.1f",
                    self.stream_link,
                    self.capture_width,
                    self.capture_height,
                    1.0 / self._emit_interval if self._emit_interval > 0 else 0.0,
                )
                return
            else:
                try:
                    cap.release()
                except Exception:
                    pass
        except Exception:
            logging.exception("Failed to open capture %s", self.stream_link)

    def _configure_fps_from_camera(self) -> None:
        """Pick a usable FPS value and update emit interval."""
        if self._target_fps and self._target_fps > 0:
            fps = float(self._target_fps)
        else:
            fps = float(self._cap.get(cv2.CAP_PROP_FPS)) if self._cap else 0.0

        if fps <= 1.0 or fps > 240.0:
            fps = 30.0

        with self._fps_lock:
            self._emit_interval = 1.0 / max(1.0, fps)

    def set_target_fps(self, fps: Optional[float]) -> None:
        """Update target FPS at runtime (software throttling only)."""
        if fps is None:
            return
        try:
            fps = float(fps)
            if fps <= 0:
                return
            with self._fps_lock:
                self._target_fps = fps
                self._emit_interval = 1.0 / max(1.0, fps)
            # Note: We don't call cap.set(CAP_PROP_FPS) here because:
            # 1. GStreamer pipelines restart when FPS is changed, causing disconnects
            # 2. Software throttling via _emit_interval is sufficient for stress management
        except Exception:
            logging.exception("set_target_fps")

    def _close_capture(self) -> None:
        """Release camera handle if open.
        
        For GStreamer captures, we add a small delay to allow the pipeline
        to properly transition through states before releasing, which helps
        avoid "Pipeline is live and does not need PREROLL" warnings and
        potential segfaults during cleanup.
        """
        try:
            if self._cap:
                # For GStreamer backend, give pipeline time to drain
                if self._using_gstreamer:
                    # Small delay helps GStreamer complete pending operations
                    time.sleep(0.05)
                self._cap.release()
                self._cap = None
                self._using_gstreamer = False
        except Exception:
            logging.debug("Exception during capture release for %s", self.stream_link)
            self._cap = None
            self._using_gstreamer = False

    def stop(self) -> None:
        """Stop capture loop and wait briefly for thread exit.
        
        The wait allows the run() loop to exit cleanly, which includes
        calling _close_capture() from within the thread context.
        If the thread doesn't stop gracefully, we terminate it forcefully.
        """
        self._running = False
        self._stop_event.set()
        
        # Wait for thread to finish (includes cleanup in run())
        if not self.wait(2000):
            logging.warning(
                "Camera %s thread did not stop in 2s, attempting terminate",
                self.stream_link
            )
            # Force terminate the thread - last resort
            self.terminate()
            # Give it a moment to actually terminate
            if not self.wait(500):
                logging.error(
                    "Camera %s thread could not be terminated - potential resource leak",
                    self.stream_link
                )
        
        # Ensure capture is closed even if thread didn't exit cleanly
        self._close_capture()
    
    def is_healthy(self) -> bool:
        """Check if the worker thread is alive and responsive.
        
        Returns True if thread is running and has emitted a frame recently.
        """
        if not self.isRunning():
            return False
        # Check if we've emitted a frame in the last 5 seconds
        if self._last_emit > 0:
            return (time.time() - self._last_emit) < 5.0
        return (time.time() - self._start_ts) < 5.0

    def get_fourcc(self) -> str:
        """Return the cached FOURCC string (thread-safe, no lock needed for reads)."""
        return self._fourcc


# ============================================================
# CAMERA DISCOVERY
# ============================================================


def test_single_camera(
    cam_index: Union[int, str],
    retries: int = 3,
    retry_delay: float = 0.2,
    allow_kill: bool = True,
    post_kill_retries: int = 2,
    post_kill_delay: float = 0.25,
) -> Optional[int]:
    """Try to open and grab a frame from one camera.

    `cam_index` accepts two forms:
      - int: a /dev/videoN index. Behavior is byte-identical to before
        device-path support -- opens `cv2.VideoCapture(cam_index,
        cv2.CAP_V4L2)` directly and returns `cam_index` on success.
      - str: a device path (e.g. a `/dev/v4l/by-path/...` symlink or a
        `/dev/videoN` path). Resolved via `os.path.realpath` FIRST; if
        the resolved node doesn't exist we fail fast (return None)
        without probing at all. `kill_device_holders` (if triggered) is
        given the RESOLVED path, since lsof/fuser can't match a symlink.
        On success we return the NUMERIC index parsed from the resolved
        `/dev/videoN` node (matching `video(\\d+)$`) so existing
        int-typed callers keep working unchanged; a resolved path that
        isn't a `/dev/videoN` node returns None (V4L2-only).
    """
    if isinstance(cam_index, str):
        resolved = os.path.realpath(cam_index)
        if not os.path.exists(resolved):
            logging.info(
                "Camera %s: resolved path %s does not exist, skipping probe",
                cam_index, resolved,
            )
            return None
        match = re.search(r"video(\d+)$", resolved)
        if not match:
            logging.warning(
                "Camera %s: resolved path %s is not a /dev/videoN node, skipping",
                cam_index, resolved,
            )
            return None
        result_index = int(match.group(1))
        open_target: Union[int, str] = resolved
        device_path = resolved
    else:
        result_index = cam_index
        open_target = cam_index
        device_path = f"/dev/video{cam_index}"

    def try_open():
        cap = cv2.VideoCapture(open_target, cv2.CAP_V4L2)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not cap.isOpened():
                return False
            if not cap.grab():
                return False
            return True
        finally:
            try:
                cap.release()
            except Exception:
                pass

    for _ in range(retries):
        if try_open():
            return result_index
        time.sleep(retry_delay)

    if allow_kill and config.KILL_DEVICE_HOLDERS:
        killed = kill_device_holders(device_path)
        if killed:
            for _ in range(post_kill_retries):
                if try_open():
                    return result_index
                time.sleep(post_kill_delay)

    return None


def get_video_indexes() -> list[int]:
    """List integer indices for /dev/video* devices."""
    video_devices = glob_module.glob("/dev/video*")
    indexes = []
    for device in sorted(video_devices):
        try:
            index = int(device.split("video")[-1])
            indexes.append(index)
        except Exception:
            logging.debug("Skipping non-numeric video device: %s", device)
    return indexes


# ============================================================
# CAMERA IDENTITY
# ============================================================
#
# Slots must not be assigned by sorted /dev/video* index: USB enumeration
# order is unstable across reboots/replugs, which can silently swap which
# physical camera lands in which tile (safety-critical for a driver-facing
# blindspot monitor). CameraIdentity anchors slot assignment to the USB
# port path reported under /dev/v4l/by-path instead, which is stable for
# a given physical cable/port regardless of enumeration order.

# Module-level so tests can monkeypatch core.camera.BY_PATH_DIR.
BY_PATH_DIR = "/dev/v4l/by-path"

# Emitted once per process the first time by-path discovery comes up
# empty while numeric /dev/video* nodes exist.
_by_path_degraded_warned = False


@dataclass(frozen=True)
class CameraIdentity:
    """Stable physical identity for one camera.

    `port_path` is the USB port path derived from the /dev/v4l/by-path
    entry name (that entry's name minus its trailing "-video-indexN"
    suffix), or the pseudo-key "index:N" in fallback mode when no by-path
    entries exist. It is the stable key: any dict/bookkeeping code
    elsewhere (slot assignment, cooldowns, etc.) MUST key on this string,
    never on a CameraIdentity instance or its `index` -- `index` and
    `device_path` are just a snapshot taken at discovery time and can go
    stale after a replug.
    """

    port_path: str
    device_path: Optional[str]
    index: int

    @property
    def stream_target(self) -> Union[int, str]:
        """Value to pass as a capture source (by-path symlink, else index)."""
        return self.device_path if self.device_path is not None else self.index


def _natural_key(s: str) -> list[Union[str, int]]:
    """Natural-sort key: splits digit runs into ints.

    Plain lexicographic sorting gets USB port paths wrong, e.g.
    "usb-0:1.10" would sort before "usb-0:1.2". Splitting digit runs out
    as ints fixes that ("1.2" < "1.10" numerically).
    """
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", s)]


def _identity_sort_key(identity: "CameraIdentity") -> tuple:
    """Sort by-path identities (natural port order) before fallback ones (by index)."""
    if identity.device_path is not None:
        return (0, _natural_key(identity.port_path))
    return (1, identity.index)


def _warn_by_path_degraded_once() -> None:
    """Log the "degraded to enumeration order" warning at most once per process."""
    global _by_path_degraded_warned
    if not _by_path_degraded_warned:
        logging.warning(
            "no /dev/v4l/by-path entries; deterministic slot binding degraded "
            "to enumeration order"
        )
        _by_path_degraded_warned = True


def list_by_path_nodes(
    by_path_dir: Optional[str] = None,
) -> dict[str, list[tuple[int, str]]]:
    """Group /dev/v4l/by-path entries by physical USB port path.

    Returns {port_path: [(video_index, entry_name), ...]}, each group's
    nodes sorted ascending by index. A port_path is a by-path entry name
    with its trailing "-video-indexN" suffix stripped. Entries that don't
    match that naming pattern, whose resolved target isn't a /dev/videoN
    node, or whose resolved target doesn't exist (a dangling symlink --
    the device was unplugged but the by-path node hasn't been cleaned up
    yet), are skipped. A missing/unreadable directory (or one with no
    matching entries) returns {}.

    `by_path_dir=None` means "use BY_PATH_DIR", read from the module
    attribute at call time so tests can monkeypatch it.
    """
    directory = by_path_dir if by_path_dir is not None else BY_PATH_DIR
    try:
        names = os.listdir(directory)
    except OSError:
        return {}

    groups: dict[str, list[tuple[int, str]]] = {}
    for name in names:
        match = re.fullmatch(r"(.+)-video-index(\d+)", name)
        if not match:
            continue
        port_path = match.group(1)
        resolved = os.path.realpath(os.path.join(directory, name))
        if not os.path.exists(resolved):
            continue
        node_match = re.search(r"video(\d+)$", resolved)
        if not node_match:
            continue
        node_index = int(node_match.group(1))
        groups.setdefault(port_path, []).append((node_index, name))

    for nodes in groups.values():
        nodes.sort(key=lambda pair: pair[0])

    return groups


def _test_identity(
    port_path: str,
    node_indexes: list[tuple[int, Optional[str]]],
    by_path_dir: Optional[str],
    **probe_kwargs: Any,
) -> Optional[CameraIdentity]:
    """Probe one physical group's candidate nodes, ascending, for the capture node.

    UVC devices commonly expose a metadata node alongside the real
    capture node in the same by-path group; the metadata node fails
    grab(). The first node (in ascending index order) that passes
    `test_single_camera` wins and becomes the identity. If every node
    fails, returns None. `node_indexes` entries are (video_index,
    entry_name); entry_name is None for fallback (no-by-path) candidates,
    which yields device_path=None.
    """
    directory = by_path_dir if by_path_dir is not None else BY_PATH_DIR
    for index, entry_name in node_indexes:
        result = test_single_camera(index, **probe_kwargs)
        if result is not None:
            device_path = (
                os.path.join(directory, entry_name) if entry_name is not None else None
            )
            return CameraIdentity(port_path=port_path, device_path=device_path, index=index)
    return None


def discover_camera_identities(by_path_dir: Optional[str] = None) -> list[CameraIdentity]:
    """Cheap, non-probing camera identity discovery (used by the rescan tick).

    Combines /dev/v4l/by-path groups with orphan /dev/video* nodes not
    covered by any by-path group. No probing is done here: a by-path
    group's LOWEST node is used as the provisional index/device_path
    (probing to pick the real capture node happens in
    find_working_camera_identities). Orphan numeric nodes become fallback
    identities (port_path "index:N", device_path None).

    Sorted: by-path identities first (natural port_path order), then
    fallback identities (by index).
    """
    directory = by_path_dir if by_path_dir is not None else BY_PATH_DIR
    groups = list_by_path_nodes(directory)

    covered_indexes: set[int] = set()
    by_path_identities: list[CameraIdentity] = []
    for port_path, nodes in groups.items():
        lowest_index, entry_name = nodes[0]
        by_path_identities.append(
            CameraIdentity(
                port_path=port_path,
                device_path=os.path.join(directory, entry_name),
                index=lowest_index,
            )
        )
        covered_indexes.update(idx for idx, _ in nodes)
    by_path_identities.sort(key=_identity_sort_key)

    all_indexes = get_video_indexes()
    if not groups and all_indexes:
        _warn_by_path_degraded_once()

    orphan_indexes = sorted(idx for idx in all_indexes if idx not in covered_indexes)
    fallback_identities = [
        CameraIdentity(port_path=f"index:{idx}", device_path=None, index=idx)
        for idx in orphan_indexes
    ]

    return by_path_identities + fallback_identities


def find_working_camera_identities(by_path_dir: Optional[str] = None) -> list[CameraIdentity]:
    """Probing camera identity discovery, used at startup.

    Mirrors find_working_cameras()'s two-round structure, but round 1
    submits one probe per PHYSICAL GROUP (via _test_identity) instead of
    per node, so a UVC metadata node never shadows the real capture node.
    Round 2 re-confirms each surviving identity's resolved index with the
    existing no-kill confirmation pattern; failures drop the identity.
    """
    directory = by_path_dir if by_path_dir is not None else BY_PATH_DIR
    groups = list_by_path_nodes(directory)

    covered_indexes = {idx for nodes in groups.values() for idx, _ in nodes}
    all_indexes = get_video_indexes()
    if not groups and all_indexes:
        _warn_by_path_degraded_once()

    orphan_indexes = sorted(idx for idx in all_indexes if idx not in covered_indexes)

    candidates: dict[str, list[tuple[int, Optional[str]]]] = dict(groups)
    for idx in orphan_indexes:
        candidates[f"index:{idx}"] = [(idx, None)]

    if not candidates:
        logging.info("No /dev/video* devices found!")
        return []

    max_workers = min(4, len(candidates))
    logging.info(
        "Testing %d camera groups concurrently (workers=%d)...",
        len(candidates),
        max_workers,
    )
    survivors: list[CameraIdentity] = []
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_test_identity, port_path, nodes, directory): port_path
            for port_path, nodes in candidates.items()
        }
        for future in as_completed(futures):
            port_path = futures[future]
            try:
                identity = future.result()
                if identity is not None:
                    with lock:
                        survivors.append(identity)
                        logging.info(
                            "Camera group %s OK -> /dev/video%d",
                            identity.port_path,
                            identity.index,
                        )
            except Exception:
                logging.exception("Exception testing camera group %s", port_path)

    # Second pass to confirm survivors without killing holders.
    if survivors:
        logging.info("Round 2 - Double-check (no pre-kill)...")
        confirmed: list[CameraIdentity] = []
        with ThreadPoolExecutor(max_workers=min(4, len(survivors))) as executor:
            futures = {
                executor.submit(
                    test_single_camera,
                    identity.index,
                    retries=2,
                    retry_delay=0.15,
                    allow_kill=False,
                ): identity
                for identity in survivors
            }
            for future in as_completed(futures):
                identity = futures[future]
                try:
                    result = future.result()
                    if result is not None:
                        confirmed.append(identity)
                        logging.info(
                            "Confirmed camera %s -> /dev/video%d",
                            identity.port_path,
                            identity.index,
                        )
                except Exception:
                    logging.exception("Exception confirming camera %s", identity.port_path)
        survivors = confirmed

    survivors.sort(key=_identity_sort_key)
    logging.info(
        "FINAL Working camera identities: %s",
        {identity.port_path: f"/dev/video{identity.index}" for identity in survivors},
    )
    return survivors


# ============================================================
# SLOT ASSIGNMENT (DETERMINISTIC TILE BINDING)
# ============================================================
#
# Pure functions: no Qt, no cv2, no I/O other than logging. SAFETY RULE
# that drives the precedence below: a pinned slot must NEVER surface a
# different camera than its pin. An honest empty/reserved tile beats
# showing the wrong camera in a safety-relevant tile, so a pin that
# matches nothing stays None and is never backfilled by another camera.


def _pin_matches(pin: str, port_path: str) -> bool:
    """Shared predicate: does `pin` (a [slots] config value) match `port_path`?

    `index:N` pins are the fallback pseudo-key form (used when there is
    no /dev/v4l/by-path) and must match `port_path` EXACTLY -- otherwise
    pin "index:1" would wrongly claim "index:10" via substring matching.
    Any other pin matches if it's a substring of `port_path` (the
    documented use is the stable "usb-0:1.3" port tail).
    """
    if pin.startswith("index:"):
        return pin == port_path
    # Substring matching below is intended for by-path port-tail pins
    # (e.g. "usb-0:1.3") only; "index:N" pins are the fallback-mode form
    # and always take the exact-match branch above.
    return pin in port_path


def assign_slots(
    identities: list[CameraIdentity],
    slot_count: int,
    pins: dict[int, str],
) -> list[Optional[CameraIdentity]]:
    """Deterministically map discovered identities to `slot_count` tiles.

    Returns exactly `slot_count` entries. Precedence:
      1. Pinned slots claim first, ascending slot index. Each pin claims
         the first unclaimed identity whose port_path matches the pin
         (see `_pin_matches`). If several identities match one pin, the
         natural-sort-first one (by port_path) wins and a WARNING
         ("ambiguous pin") is logged.
      2. Remaining identities (in their given order -- discovery order is
         already sorted) fill the remaining UNPINNED slots, ascending.
      3. A pinned slot whose pin matched nothing stays None -- NEVER
         backfilled by an unpinned camera (safety rule above).
      4. Identities left over once all slots are filled/reserved are
         dropped, with an INFO log.
    """
    slots: list[Optional[CameraIdentity]] = [None] * slot_count
    claimed: set[int] = set()  # id() of identities already placed in a slot

    for slot_index in sorted(pins):
        if not (0 <= slot_index < slot_count):
            continue
        pin = pins[slot_index]
        matches = [
            identity
            for identity in identities
            if id(identity) not in claimed and _pin_matches(pin, identity.port_path)
        ]
        if not matches:
            continue
        matches.sort(key=lambda identity: _natural_key(identity.port_path))
        winner = matches[0]
        if len(matches) > 1:
            logging.warning(
                "ambiguous pin: slot%d=%r matches %d cameras, using %s",
                slot_index, pin, len(matches), winner.port_path,
            )
        slots[slot_index] = winner
        claimed.add(id(winner))

    unpinned_slot_indexes = [i for i in range(slot_count) if i not in pins]
    remaining = [identity for identity in identities if id(identity) not in claimed]

    for slot_index, identity in zip(unpinned_slot_indexes, remaining):
        slots[slot_index] = identity
        claimed.add(id(identity))

    dropped = len(remaining) - len(unpinned_slot_indexes)
    if dropped > 0:
        logging.info("%d discovered camera(s) dropped: no free slot", dropped)

    return slots


def choose_slot_for_identity(
    identity: CameraIdentity,
    free_slot_indexes: list[int],
    pins: dict[int, str],
    last_slot_by_port: dict[str, int],
) -> Optional[int]:
    """Pick a slot for one identity during rescan/reattach (pure, no side effects).

    Precedence, first hit wins:
      1. A free PINNED slot whose pin matches `identity.port_path` (lowest
         index if several match).
      2. `last_slot_by_port[identity.port_path]`, if that slot is free AND
         is either unpinned or its pin still matches this identity -- a
         replug returns to its previous tile, but never steals a slot now
         reserved for a different port.
      3. The lowest free UNPINNED slot.
      4. None -- the only free slots are pinned to other ports, so the
         camera waits rather than stealing a reserved tile.
    """
    free_set = set(free_slot_indexes)

    pinned_matches = sorted(
        slot_index
        for slot_index in free_set
        if slot_index in pins and _pin_matches(pins[slot_index], identity.port_path)
    )
    if pinned_matches:
        return pinned_matches[0]

    last_slot = last_slot_by_port.get(identity.port_path)
    if last_slot is not None and last_slot in free_set:
        pin = pins.get(last_slot)
        if pin is None or _pin_matches(pin, identity.port_path):
            return last_slot

    unpinned_free = sorted(slot_index for slot_index in free_set if slot_index not in pins)
    if unpinned_free:
        return unpinned_free[0]

    return None


def find_working_cameras() -> list[int]:
    """Return a list of camera indices that can capture frames.

    Delegates to find_working_camera_identities() (by-path aware, dedupes
    UVC metadata nodes from the real capture node per physical port) and
    returns just the resolved /dev/videoN indices, for callers that don't
    need full CameraIdentity objects.
    """
    return [identity.index for identity in find_working_camera_identities()]
