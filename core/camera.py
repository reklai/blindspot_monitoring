"""Camera capture, device discovery, stable identity, and slot assignment."""

from __future__ import annotations

import glob as glob_module
import logging
import os
import platform
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Optional, Union

import cv2
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from core import config
from utils import kill_device_holders


# OpenCV build capabilities do not change while the process is running.
_gstreamer_available: Optional[bool] = None


def _check_gstreamer_available() -> bool:
    """Best-effort parse and cache OpenCV's reported GStreamer availability.

    The parser recognizes the build-info form whose final token is ``YES``.
    Missing, unrecognized, or unreadable build information is treated as
    unavailable so capture can use the fallback path.
    """
    global _gstreamer_available
    if _gstreamer_available is not None:
        return _gstreamer_available
    
    try:
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
    """Build a low-latency MJPEG ``v4l2src`` pipeline for a local device.

    Integer indexes become ``/dev/videoN`` paths; absolute ``/dev`` paths are
    used as given. Other strings return ``None`` because URLs and general file
    paths are not valid inputs to this V4L2-only pipeline.
    """
    if isinstance(device, int):
        device_arg = f"/dev/video{device}"
    elif isinstance(device, str) and device.startswith("/dev/"):
        device_arg = device
    else:
        return None

    # Both the leaky queue and one-buffer appsink discard stale frames. The
    # dashboard values low latency over processing every frame from the camera.
    return (
        f"v4l2src device={device_arg} ! "
        f"image/jpeg,width={width},height={height} ! "
        f"queue max-size-buffers=2 leaky=downstream ! "
        f"jpegdec ! videoconvert ! "
        f"appsink drop=1 max-buffers=1 sync=false"
    )


class CaptureWorker(QThread):
    """Capture frames on a dedicated thread that owns the camera handle."""
    
    # Consumers receive complete BGR arrays; capture status changes only on
    # transitions so the UI does not process duplicate online/offline events.
    frame_ready = pyqtSignal(object)
    status_changed = pyqtSignal(bool)

    def __init__(
        self,
        stream_link: Union[int, str],
        parent: Optional[QObject] = None,
        target_fps: Optional[float] = None,
        capture_width: Optional[int] = None,
        capture_height: Optional[int] = None,
        ui_fps: Optional[float] = None,
    ) -> None:
        """Initialize capture configuration without opening the device.

        ``target_fps`` is requested from V4L2 when the handle opens and also
        seeds the software emission throttle. ``ui_fps`` limits emissions to
        the widget's render rate. For the supported rates of at least 1 FPS, the
        effective emit rate is the lower of the capture rate estimate and the
        UI bound.
        """
        super().__init__(parent)
        self.stream_link = stream_link
        self._running = True
        self._reconnect_backoff = 1.0
        self._cap: Optional[cv2.VideoCapture] = None
        self._last_emit = 0.0
        # Emission deadline owned exclusively by the ``run`` thread; ``_last_emit``
        # stays the health-check timestamp of the last successful emission.
        self._next_emit_due = 0.0
        self._target_fps = target_fps
        # ``None`` leaves the emit rate bounded only by the capture rate estimate.
        self._ui_fps = float(ui_fps) if (ui_fps and ui_fps > 0) else None
        # The open handle supplies a better estimate when no target was requested.
        self._device_fps = (
            float(target_fps) if (target_fps and target_fps > 0) else 30.0
        )
        self._recompute_emit_interval_locked()
        self.capture_width = capture_width
        self.capture_height = capture_height
        self._online = False
        self._start_ts = time.time()
        self._open_fail_count = 0
        # GStreamer handles need a short settling period before release.
        self._using_gstreamer = False
        # The capture worker replaces this snapshot after each successful open.
        self._fourcc: str = "unknown"
        # Dynamic FPS setters run on the UI thread while ``run`` reads the interval.
        self._fps_lock = threading.Lock()
        self._stop_event = threading.Event()
        # See ``stop``: releasing a handle still in use can crash the process.
        self._leaked = False

    def run(self) -> None:
        """Capture frames until stopped, reopening the device after failures."""
        self._start_ts = time.time()
        self._stop_event.clear()
        logging.info("Camera %s thread started", self.stream_link)
        while self._running:
            try:
                # Failed opens back off, but a successful reconnect resets the delay.
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

                # Always drain one driver frame. Decoding is deferred to ``retrieve``
                # so throttled frames are cheap on the V4L2 MJPEG path.
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

                now = time.time()
                # Throttle before ``retrieve`` to avoid JPEG decoding frames the UI
                # cannot render. GStreamer has already decoded them in its pipeline.
                if self._emit_due(now):
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
                    # OpenCV returns a frame array owned by this call, so the queued
                    # UI consumer can retain it without an additional copy.
                    self.frame_ready.emit(frame)
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
        """Resolve the configured source for the current open attempt.

        Integer sources pass through unchanged. String sources are deliberately
        resolved on every reconnect rather than cached: after a USB replug, the
        same ``/dev/v4l/by-path`` link may point to a new ``/dev/videoN`` node.
        If resolution raises, returning the original value lets the normal open
        failure and retry path handle it.
        """
        if isinstance(self.stream_link, int):
            return self.stream_link
        try:
            return os.path.realpath(self.stream_link)
        except Exception:
            return self.stream_link

    def _open_capture(self) -> None:
        """Open the source through the preferred backend cascade and configure it."""
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

            # GStreamer is preferred for local MJPEG devices. Unsupported source
            # forms produce no pipeline and proceed directly to the OpenCV cascade.
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
                        # An open pipeline is not usable until its source yields data.
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

            # Some cameras reject a forced format, so try MJPG, then YUYV, then
            # driver-selected format. Each attempt also verifies an initial grab.
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

    def _emit_due(self, now: float) -> bool:
        """Decide whether to emit the frame grabbed at ``now``.

        The deadline advances by whole intervals so credit past a deadline
        carries into the next period; resetting the phase to the emission
        time would quantize non-divisor capture/UI rate pairs down an entire
        frame period. A deadline more than one interval behind ``now`` means
        the stream stalled, so the cadence restarts from ``now`` rather than
        emitting a burst against the banked backlog.
        """
        with self._fps_lock:
            emit_interval = self._emit_interval
        if now < self._next_emit_due:
            return False
        deadline = self._next_emit_due + emit_interval
        if deadline < now:
            deadline = now + emit_interval
        self._next_emit_due = deadline
        return True

    def _recompute_emit_interval_locked(self) -> None:
        """Derive the emit interval from the capture and render rate bounds.

        The caller must hold ``_fps_lock``, except during single-threaded
        construction before the capture worker can start. Rates are floored at
        1 FPS; application callers provide bounds at or above that floor, which
        preserves the invariant that emit rate cannot exceed render rate.
        """
        fps = self._device_fps
        if self._ui_fps is not None:
            fps = min(fps, self._ui_fps)
        self._emit_interval = 1.0 / max(1.0, fps)

    def _configure_fps_from_camera(self) -> None:
        """Refresh the capture rate estimate used by the emission throttle.

        A requested target takes precedence over the driver's reported value.
        Implausible or unavailable driver values fall back to 30 FPS.
        """
        if self._target_fps and self._target_fps > 0:
            fps = float(self._target_fps)
        else:
            fps = float(self._cap.get(cv2.CAP_PROP_FPS)) if self._cap else 0.0

        if fps <= 1.0 or fps > 240.0:
            fps = 30.0

        with self._fps_lock:
            self._device_fps = fps
            self._recompute_emit_interval_locked()

    def set_target_fps(self, fps: Optional[float]) -> None:
        """Update the runtime capture rate estimate and software emit throttle.

        This does not reconfigure an open device. Restarting a GStreamer pipeline
        for a rate change causes disconnects, while throttling emissions is enough
        for the stress controller. The UI-rate bound remains in force.
        """
        if fps is None:
            return
        try:
            fps = float(fps)
            if fps <= 0:
                return
            with self._fps_lock:
                self._target_fps = fps
                self._device_fps = fps
                self._recompute_emit_interval_locked()
        except Exception:
            logging.exception("set_target_fps")

    def set_ui_fps(self, ui_fps: Optional[float]) -> None:
        """Update the render rate bound after the widget changes its UI FPS."""
        if ui_fps is None:
            return
        try:
            ui_fps = float(ui_fps)
            if ui_fps <= 0:
                return
            with self._fps_lock:
                self._ui_fps = ui_fps
                self._recompute_emit_interval_locked()
        except Exception:
            logging.exception("set_ui_fps")

    def _close_capture(self) -> None:
        """Release the capture worker's camera handle.

        GStreamer gets a short settling delay before release. This reduces
        cleanup warnings and crashes while pipeline operations are still pending.
        """
        try:
            if self._cap:
                if self._using_gstreamer:
                    # Let pending pipeline operations settle before teardown.
                    time.sleep(0.05)
                self._cap.release()
                self._cap = None
                self._using_gstreamer = False
        except Exception:
            logging.debug("Exception during capture release for %s", self.stream_link)
            self._cap = None
            self._using_gstreamer = False

    @property
    def is_leaked(self) -> bool:
        """Whether ``stop`` timed out while the capture thread still owned its handle."""
        return self._leaked

    def stop(self) -> bool:
        """Stop the thread and release its device only when release is safe.

        Returns ``True`` once the thread is confirmed stopped and the handle is
        closed. Returns ``False`` when termination fails; in that case the handle
        is intentionally left with the live thread and ``is_leaked`` becomes true.

        ``VideoCapture.release`` must not race with ``grab`` or ``retrieve`` in
        another thread because OpenCV may segfault. A graceful exit closes its own
        handle. If forced termination is confirmed, this method closes any handle
        the abandoned cleanup missed. If the thread remains alive, no code in this
        process can safely reclaim its descriptor: rescan probes do not kill
        holders, and holder cleanup excludes this process. The parked worker may
        still unblock later, exit ``run``, and release its own handle. Otherwise,
        recovery requires a replug or process restart; detaching the widget only
        frees its UI slot.
        """
        self._running = False
        self._stop_event.set()

        # ``run`` normally closes the handle; this is a safe idempotent fallback.
        if self.wait(2000):
            self._close_capture()
            return True

        logging.warning(
            "Camera %s thread did not stop in 2s, attempting terminate",
            self.stream_link,
        )
        # Forced termination is a last resort after cooperative shutdown stalls.
        self.terminate()
        # Only a confirmed stop makes cross-thread release safe.
        if self.wait(500) or not self.isRunning():
            self._close_capture()
            return True

        # Preserve process safety: the live thread may still be inside OpenCV.
        logging.error(
            "Camera %s capture thread could not be terminated; leaking its fd "
            "(held by our own zombie thread -- not reclaimable in-process). "
            "Recover by physically replugging the camera or restarting.",
            self.stream_link,
        )
        self._leaked = True
        return False

    def is_healthy(self) -> bool:
        """Return whether the worker thread is alive and within its frame window.

        The initial five-second grace starts when ``run`` starts. After the first
        frame, health instead requires an emission within the last five seconds.
        This does not report the camera's current online status.
        """
        if not self.isRunning():
            return False
        if self._last_emit > 0:
            return (time.time() - self._last_emit) < 5.0
        return (time.time() - self._start_ts) < 5.0

    def get_fourcc(self) -> str:
        """Return the latest immutable FOURCC snapshot, or ``"unknown"``."""
        return self._fourcc


# Capture probing


def test_single_camera(
    cam_index: Union[int, str],
    retries: int = 3,
    retry_delay: float = 0.2,
    allow_kill: bool = True,
    post_kill_retries: int = 2,
    post_kill_delay: float = 0.25,
) -> Optional[int]:
    """Probe one V4L2 source and return its numeric index when usable.

    Integer inputs are opened directly. String inputs are resolved first and
    must point to an existing ``/dev/videoN``-style node; successful string
    probes still return ``N`` to keep the result type uniform. The resolved path
    is also used for optional holder cleanup because device tools do not match
    the by-path symlink reliably.

    After the normal retries fail, holder cleanup and the shorter post-cleanup
    retry sequence run only when both ``allow_kill`` and
    ``KILL_DEVICE_HOLDERS`` are enabled.
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
    """Return numeric suffixes from the current ``/dev/video*`` nodes."""
    video_devices = glob_module.glob("/dev/video*")
    indexes = []
    for device in sorted(video_devices):
        try:
            index = int(device.split("video")[-1])
            indexes.append(index)
        except Exception:
            logging.debug("Skipping non-numeric video device: %s", device)
    return indexes


# Stable camera identity
#
# ``/dev/videoN`` indexes can change after a reboot or replug. Slot assignment
# therefore follows the USB port path so the dashboard does not silently
# show a different camera in a safety-relevant position.

# Kept configurable at module scope for alternate discovery roots and tests.
BY_PATH_DIR = "/dev/v4l/by-path"

# Avoid repeating the same degraded-identity warning on every rescan.
_by_path_degraded_warned = False


@dataclass(frozen=True)
class CameraIdentity:
    """Represent a by-path camera group or an ungrouped numeric capture node.

    For a grouped identity, ``port_path`` is the by-path entry name without
    ``-video-indexN`` and provides the stable bookkeeping key for slot
    assignment, cooldowns, and reattach memory. ``device_path`` is the by-path
    symlink that the capture worker re-resolves on each open, while ``index`` is
    the discovery-time ``/dev/videoN`` snapshot.

    A numeric node without a by-path entry instead uses ``index:N`` as a
    degraded key and has no ``device_path``. Such a fallback is not grouped by
    physical camera, and its key and index may change after a replug.
    """

    port_path: str
    device_path: Optional[str]
    index: int

    @property
    def stream_target(self) -> Union[int, str]:
        """Return the stable by-path source, or the fallback numeric index."""
        return self.device_path if self.device_path is not None else self.index


def _natural_key(s: str) -> list[Union[str, int]]:
    """Build a key that orders numeric path components by numeric value.

    For example, USB port ``1.2`` sorts before ``1.10``.
    """
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", s)]


def _identity_sort_key(identity: "CameraIdentity") -> tuple:
    """Order stable by-path identities first, then numeric fallbacks."""
    if identity.device_path is not None:
        return (0, _natural_key(identity.port_path))
    return (1, identity.index)


def _warn_by_path_degraded_once() -> None:
    """Warn once that slot order is falling back to capture-node indexes."""
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
    """Group valid by-path entries by their USB port path.

    The result is ``{port_path: [(video_index, entry_name), ...]}``, with each
    group ordered by its resolved ``/dev/videoN`` index. Malformed entries,
    dangling links, and links whose targets are not video nodes are ignored.
    Missing, unreadable, or empty directories return an empty mapping.

    With no explicit directory, ``BY_PATH_DIR`` is read at call time.
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
    """Return the first working capture node in one USB port path group.

    UVC cameras often expose metadata and capture nodes under the same port.
    Probing in ascending index order filters out metadata nodes that cannot
    grab frames. ``node_indexes`` contains ``(video_index, entry_name)`` pairs;
    a ``None`` entry name represents a numeric fallback without a by-path link.
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


def probe_group_fallback(
    port_path: str,
    exclude_index: int,
    **probe_kwargs: Any,
) -> Optional[CameraIdentity]:
    """Probe the remaining nodes after a rescan's provisional node fails.

    Non-probing discovery selects a group's lowest node, which may be a UVC
    metadata node. This reloads the group from ``BY_PATH_DIR``, excludes the
    node already tested, and applies the same ascending probe used at startup.
    Numeric fallback identities have no group to expand and return ``None``.
    Additional keyword arguments are forwarded to ``test_single_camera``.
    """
    nodes = list_by_path_nodes().get(port_path)
    if not nodes:
        return None
    remaining: list[tuple[int, Optional[str]]] = [
        (idx, name) for idx, name in nodes if idx != exclude_index
    ]
    if not remaining:
        return None
    return _test_identity(port_path, remaining, None, **probe_kwargs)


def discover_camera_identities(by_path_dir: Optional[str] = None) -> list[CameraIdentity]:
    """Build the inexpensive identity snapshot used by periodic rescans.

    Each by-path group contributes its lowest node as a provisional capture
    node; this function does not open devices. Numeric nodes not represented by
    a group become ``index:N`` fallback identities. Results place by-path
    identities in natural USB port path order before index-ordered fallbacks.
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
    """Discover and verify working camera identities for startup.

    The first concurrent pass probes one USB port path group at a time, allowing
    ``_test_identity`` to distinguish a real capture node from sibling metadata
    nodes. Numeric fallback nodes cannot be grouped by physical camera and are
    probed independently. A second pass rechecks each survivor without killing
    device holders; an unstable candidate that fails confirmation is omitted.
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

    # Confirmation must be non-destructive: another failure drops the candidate.
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


# Deterministic slot assignment
#
# These helpers have no capture or Qt side effects. An unmatched pinned slot
# remains a placeholder: showing no camera is safer than showing the wrong
# physical camera in a safety-relevant slot.


def _pin_matches(pin: str, port_path: str) -> bool:
    """Return whether a configured pin identifies this USB port path.

    ``index:N`` fallbacks require exact equality so ``index:1`` cannot claim
    ``index:10``. By-path pins may be stable port tails such as
    ``usb-0:1.3``, but matches must start and end at component boundaries.
    This prevents a pin for port ``1.1`` from claiming ``1.10``, a nested
    ``1.1.2`` port, or the trailing fragment of another dotted port path.
    """
    if pin.startswith("index:"):
        return pin == port_path
    return (
        re.search(
            r"(?<![0-9A-Za-z.])" + re.escape(pin) + r"(?=:|$)", port_path
        )
        is not None
    )


def assign_slots(
    identities: list[CameraIdentity],
    slot_count: int,
    pins: dict[int, str],
) -> list[Optional[CameraIdentity]]:
    """Map startup identities to a fixed-length list of camera slots.

    Pinned slots claim matching identities first in slot order. If one pin
    matches multiple identities, natural USB port path order selects the winner
    and a warning records the ambiguity. Remaining identities retain their
    input order while filling unpinned slots. Unmatched pinned slots stay
    ``None`` placeholders, and identities beyond available slots are logged and
    omitted.
    """
    slots: list[Optional[CameraIdentity]] = [None] * slot_count
    # Track object identity so one discovered record cannot occupy two slots.
    claimed: set[int] = set()

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
    """Choose a free slot for one discovered identity without mutating the inputs.

    A matching pinned slot has priority, followed by the identity's remembered
    slot when it is still compatible, then the lowest unpinned slot. ``None``
    means every free slot has a nonmatching reservation, so the identity waits
    for a later rescan instead of taking a reserved slot.
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
    """Return capture-node indexes for callers that do not need identities.

    By-path discovery groups sibling UVC nodes and returns the first working
    capture node from each USB port path. Numeric fallback nodes cannot be
    grouped by physical camera, so each working orphan is returned independently.
    """
    return [identity.index for identity in find_working_camera_identities()]
