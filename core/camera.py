"""
Camera capture and discovery.

``CaptureWorker`` owns one camera on one QThread. It opens the device
(GStreamer first when allowed, then V4L2 with MJPG, YUYV and automatic
format), pulls frames as fast as the driver delivers them, and emits only
the frames the dashboard asked for. Everything that touches the
``cv2.VideoCapture`` handle runs on the worker thread; the UI thread only
flips flags, adjusts the target rate under ``_fps_lock`` and reads cached
strings.

Discovery (``find_working_cameras``) runs before the UI exists. It probes
every ``/dev/video*`` node concurrently, optionally evicting whatever holds
a busy device, then re-probes the survivors without eviction so a camera
that only opened because we killed its holder is confirmed rather than
assumed.
"""

from __future__ import annotations

import glob as glob_module
import logging
import platform
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Union

import cv2
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from core import config
from core.throttle import FrameThrottle
from utils import kill_device_holders

StreamLink = Union[int, str]

_gstreamer_available: Optional[bool] = None
_GSTREAMER_BUILD_LINE = re.compile(r"^\s*GStreamer\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


def gstreamer_available() -> bool:
    """Return True if this OpenCV build was compiled with GStreamer.

    Parsed once from ``cv2.getBuildInformation()`` and cached: the answer
    cannot change while the process runs.

    History: the previous check compared the *last* token of the build line
    with ``YES``. Debian's OpenCV prints ``GStreamer: YES (1.22.0)``, so the
    last token was the version and the check always said no; fielded units
    have therefore only ever run the V4L2 path. This function answers
    correctly, which is why ``use_gstreamer`` now defaults to false in
    config.ini: enabling the pipeline is a deliberate, testable step.
    """
    global _gstreamer_available
    if _gstreamer_available is None:
        try:
            match = _GSTREAMER_BUILD_LINE.search(cv2.getBuildInformation())
            _gstreamer_available = bool(match) and match.group(1).upper() == "YES"
        except Exception:
            _gstreamer_available = False
            logging.debug("Could not check GStreamer availability", exc_info=True)
        logging.info(
            "GStreamer support %s in OpenCV build",
            "detected" if _gstreamer_available else "not available",
        )
    return bool(_gstreamer_available)


def gstreamer_pipeline(device_index: int, width: int, height: int) -> str:
    """Build the low-latency MJPEG pipeline used for USB cameras.

    ``queue leaky=downstream`` and ``appsink drop=1 max-buffers=1`` together
    guarantee the sink always holds the newest frame and never blocks the
    source, which is what a live monitor wants. ``jpegdec`` is the software
    decoder; there is no hardware JPEG path on the Pi through this route.
    """
    return (
        f"v4l2src device=/dev/video{device_index} ! "
        f"image/jpeg,width={width},height={height} ! "
        "queue max-size-buffers=2 leaky=downstream ! "
        "jpegdec ! videoconvert ! "
        "appsink drop=1 max-buffers=1 sync=false"
    )


def _release_quietly(cap: Optional[cv2.VideoCapture]) -> None:
    if cap is None:
        return
    try:
        cap.release()
    except Exception:
        pass


def _fourcc_to_str(raw: float) -> str:
    code = int(raw)
    return "".join(chr((code >> (8 * i)) & 0xFF) for i in range(4))


class CaptureWorker(QThread):
    """Background thread that captures frames from one camera.

    Lifecycle: construct, ``start()``, ``stop()``. A worker is single-use;
    once stopped it is discarded and the tile builds a new one. Nothing in
    the codebase restarts a stopped worker, and ``_stop_event`` is never
    cleared, so do not try.

    Threads: ``run``/``_step``/``_open_*``/``_close_capture`` execute on the
    worker thread and are the only code that touches the ``cv2.VideoCapture``.
    ``set_target_fps``, ``stop``, ``is_healthy``, ``get_fourcc`` and the
    properties are for the UI thread. Signals are emitted from the worker
    thread; Qt queues them, so connected slots run on the UI thread.

    Frame ownership: every emitted frame is a fresh array (``retrieve()``
    allocates per call; verified, not assumed). After ``emit`` the worker
    holds no reference, so the receiver may keep it as long as it likes.
    """

    frame_ready = pyqtSignal(object)
    status_changed = pyqtSignal(bool)

    #: Seconds to wait for the loop to exit before terminating the thread.
    STOP_TIMEOUT_MS = 2000
    #: Reconnect back-off bounds after a failed open.
    RECONNECT_MIN_SEC = 1.0
    RECONNECT_MAX_SEC = 10.0
    #: Emit-silence after which ``is_healthy`` reports the worker stalled.
    HEALTHY_SILENCE_SEC = 5.0
    #: Fallback when the camera reports no usable frame rate.
    DEFAULT_CAMERA_FPS = 30.0

    def __init__(
        self,
        stream_link: StreamLink,
        parent: Optional[QObject] = None,
        target_fps: Optional[float] = None,
        capture_width: Optional[int] = None,
        capture_height: Optional[int] = None,
    ) -> None:
        super().__init__(parent)
        self.stream_link = stream_link
        self.capture_width = capture_width
        self.capture_height = capture_height

        self._target_fps: Optional[float] = target_fps if target_fps and target_fps > 0 else None
        self._fps_lock = threading.Lock()
        self._throttle = FrameThrottle(self._target_fps or self.DEFAULT_CAMERA_FPS)

        self._stop_event = threading.Event()
        self._cap: Optional[cv2.VideoCapture] = None
        self._backend = "V4L2"
        self._online = False
        self._open_fail_count = 0
        self._reconnect_backoff = self.RECONNECT_MIN_SEC
        self._start_ts = time.time()
        self._last_emit = 0.0
        # Written by the worker thread, read by the UI thread; a str swap is atomic.
        self._fourcc = "unknown"
        # Lifetime counters for diagnostics (frames pulled from the driver /
        # frames handed to the UI). Plain ints: torn reads are impossible.
        self.grab_count = 0
        self.emit_count = 0

    # ------------------------------------------------------------------
    # UI-thread API
    # ------------------------------------------------------------------

    @property
    def target_fps(self) -> Optional[float]:
        return self._target_fps

    @property
    def emit_interval(self) -> float:
        """Seconds between emitted frames at the current target rate."""
        with self._fps_lock:
            return self._throttle.interval

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def set_target_fps(self, fps: Optional[float]) -> None:
        """Change the emit rate at runtime.

        This is software throttling only. The device is deliberately not
        reconfigured: changing CAP_PROP_FPS restarts a GStreamer pipeline and
        drops the connection, and the throttle alone is enough to shed load.
        """
        if fps is None:
            return
        try:
            fps = float(fps)
        except (TypeError, ValueError):
            return
        if fps <= 0:
            return
        with self._fps_lock:
            self._target_fps = fps
            self._throttle.set_fps(fps)

    def stop(self) -> None:
        """Ask the loop to exit and wait for it.

        The loop closes the capture itself on the way out. If it does not
        exit in time we terminate the thread and release the handle from
        here; that is a last resort because releasing a live GStreamer
        pipeline from another thread is not guaranteed safe.
        """
        self._stop_event.set()
        if not self.wait(self.STOP_TIMEOUT_MS):
            logging.warning(
                "Camera %s thread did not stop in %ds, attempting terminate",
                self.stream_link,
                self.STOP_TIMEOUT_MS // 1000,
            )
            self.terminate()
            if not self.wait(500):
                logging.error(
                    "Camera %s thread could not be terminated - potential resource leak",
                    self.stream_link,
                )
        self._close_capture()

    def is_healthy(self) -> bool:
        """True if the thread runs and emitted a frame (or started) recently."""
        if not self.isRunning():
            return False
        since = self._last_emit if self._last_emit > 0 else self._start_ts
        return (time.time() - since) < self.HEALTHY_SILENCE_SEC

    def get_fourcc(self) -> str:
        """Pixel format the device settled on, for status logs."""
        return self._fourcc

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._start_ts = time.time()
        logging.info("Camera %s thread started", self.stream_link)
        while not self._stop_event.is_set():
            try:
                self._step()
            except Exception:
                logging.exception("Exception in CaptureWorker %s", self.stream_link)
                self._stop_event.wait(0.2)
        self._set_online(False)
        self._close_capture()
        logging.info("Camera %s thread stopped", self.stream_link)

    def _step(self) -> None:
        """One iteration of the capture loop: ensure open, grab, maybe emit."""
        cap = self._cap
        if cap is None or not cap.isOpened():
            if not self._open_capture():
                self._note_open_failure()
                self._set_online(False)
                self._stop_event.wait(self._reconnect_backoff)
                self._reconnect_backoff = min(self._reconnect_backoff * 1.5, self.RECONNECT_MAX_SEC)
                return
            self._reconnect_backoff = self.RECONNECT_MIN_SEC
            self._open_fail_count = 0
            self._set_online(True)
            cap = self._cap
            assert cap is not None

        # grab() dequeues the newest buffer so the driver never backs up;
        # retrieve() is where the (MJPEG) decode and the copy happen, so it is
        # only paid for frames that will actually be emitted. grab() blocks
        # until the driver has a frame, which is what paces this loop.
        if not cap.grab():
            logging.debug("Camera %s: grab() failed, closing capture", self.stream_link)
            self._close_capture()
            self._set_online(False)
            return
        self.grab_count += 1

        now = time.time()
        with self._fps_lock:
            due = self._throttle.accept(now)
        if due:
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                logging.debug("Camera %s: retrieve() failed, closing capture", self.stream_link)
                self._close_capture()
                self._set_online(False)
                return
            self._last_emit = now
            self.emit_count += 1
            # Queued to the UI thread. The array is not copied; Qt just
            # carries the reference, and the tile keeps it until the next
            # frame replaces it.
            self.frame_ready.emit(frame)

        # A short yield so the UI thread and the other camera threads get the
        # GIL between frames; grab() already releases it while blocking.
        self.msleep(1)

    def _note_open_failure(self) -> None:
        self._open_fail_count += 1
        if self._open_fail_count % 10 == 0:
            logging.warning(
                "Camera %s open failed (%d attempts)",
                self.stream_link,
                self._open_fail_count,
            )

    def _set_online(self, online: bool) -> None:
        if self._online != online:
            self._online = online
            self.status_changed.emit(online)

    # ------------------------------------------------------------------
    # Opening the device
    # ------------------------------------------------------------------

    def _open_capture(self) -> bool:
        """Open the camera through the first backend that delivers a frame.

        Order: GStreamer (decodes in a dedicated pipeline thread, lower
        latency), then V4L2 asking for MJPG (fits USB 2.0 bandwidth at
        640x480), then YUYV (no decode but 2 bytes/pixel over USB), then
        whatever the driver picks. Each rung must actually deliver a frame
        via grab(); isOpened() alone is true for devices that never produce
        one, such as a UVC metadata node.
        """
        try:
            cap: Optional[cv2.VideoCapture] = None
            backend = "V4L2"
            if self._gstreamer_eligible():
                cap = self._open_gstreamer()
                if cap is not None:
                    backend = "GStreamer"
                else:
                    logging.info(
                        "Camera %s: GStreamer unavailable, falling back to V4L2",
                        self.stream_link,
                    )
            if cap is None:
                for fourcc in ("MJPG", "YUYV", None):
                    logging.info("Camera %s: trying V4L2 %s", self.stream_link, fourcc or "auto")
                    cap = self._open_v4l2(fourcc)
                    if cap is not None:
                        break
            if cap is None:
                logging.warning(
                    "Camera %s: Failed to open capture (no backend worked)",
                    self.stream_link,
                )
                return False

            self._cap = cap
            self._backend = backend
            self._apply_camera_fps(cap)
            self._log_opened(cap, backend)
            return True
        except Exception:
            logging.exception("Failed to open capture %s", self.stream_link)
            return False

    def _gstreamer_eligible(self) -> bool:
        return (
            config.USE_GSTREAMER
            and gstreamer_available()
            and platform.system() == "Linux"
            and isinstance(self.stream_link, int)
        )

    def _open_gstreamer(self) -> Optional[cv2.VideoCapture]:
        width = int(self.capture_width or 640)
        height = int(self.capture_height or 480)
        pipeline = gstreamer_pipeline(int(self.stream_link), width, height)
        cap: Optional[cv2.VideoCapture] = None
        # The whole attempt is guarded, not just the constructor: a pipeline
        # that opens but throws inside grab() must still fall through to the
        # V4L2 rungs rather than fail this open cycle outright.
        try:
            cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
            if not cap or not cap.isOpened() or not cap.grab():
                _release_quietly(cap)
                return None
        except Exception as exc:
            logging.warning("GStreamer failed for camera %s: %s", self.stream_link, exc)
            _release_quietly(cap)
            return None
        logging.info("GStreamer pipeline opened for camera %s (jpegdec)", self.stream_link)
        return cap

    def _open_v4l2(self, fourcc: Optional[str]) -> Optional[cv2.VideoCapture]:
        backend = cv2.CAP_V4L2 if platform.system() == "Linux" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.stream_link, backend)
        if not cap or not cap.isOpened():
            _release_quietly(cap)
            return None

        # Property sets are best-effort: drivers reject what they cannot do
        # and OpenCV may raise for unsupported properties on some builds.
        # BUFFERSIZE 1 keeps the driver queue shallow so a slow consumer sees
        # the newest frame, not a backlog. The 2 s timeouts stop a dying
        # device from blocking this thread indefinitely in open/read.
        def try_set(prop: int, value: float) -> None:
            try:
                cap.set(prop, value)
            except Exception:
                pass

        if fourcc:
            try_set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        if self.capture_width:
            try_set(cv2.CAP_PROP_FRAME_WIDTH, int(self.capture_width))
        if self.capture_height:
            try_set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.capture_height))
        try_set(cv2.CAP_PROP_BUFFERSIZE, 1)
        try_set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 2000)
        try_set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2000)
        try_set(cv2.CAP_PROP_FPS, float(self._target_fps) if self._target_fps else 0)

        if not cap.grab():
            _release_quietly(cap)
            return None
        return cap

    def _apply_camera_fps(self, cap: cv2.VideoCapture) -> None:
        """Seed the throttle from the requested rate, else the camera's own."""
        fps = self._target_fps
        if not fps:
            try:
                fps = float(cap.get(cv2.CAP_PROP_FPS))
            except Exception:
                fps = 0.0
        # Drivers report 0, -1 or absurd values when they do not know;
        # anything outside a sane camera range means "unknown".
        if fps <= 1.0 or fps > 240.0:
            fps = self.DEFAULT_CAMERA_FPS
        with self._fps_lock:
            self._throttle.set_fps(fps)
            self._throttle.reset()

    def _log_opened(self, cap: cv2.VideoCapture, backend: str) -> None:
        try:
            self._fourcc = _fourcc_to_str(cap.get(cv2.CAP_PROP_FOURCC))
            if self._fourcc.strip() and self._fourcc != "MJPG":
                logging.info("Camera %s using FOURCC=%s", self.stream_link, self._fourcc)
            logging.info(
                "Camera %s format %dx%d @ %.1f FPS (%s)",
                self.stream_link,
                int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                float(cap.get(cv2.CAP_PROP_FPS)),
                backend,
            )
        except Exception:
            pass
        logging.info(
            "Opened capture %s (requested %sx%s) -> emit fps=%.1f",
            self.stream_link,
            self.capture_width,
            self.capture_height,
            self.emit_interval and 1.0 / self.emit_interval,
        )

    def _close_capture(self) -> None:
        """Release the device handle, if any, from the calling thread."""
        cap, self._cap = self._cap, None
        if cap is None:
            return
        try:
            if self._backend == "GStreamer":
                # Let the pipeline finish in-flight buffers before teardown;
                # releasing immediately can crash inside GStreamer.
                time.sleep(0.05)
            cap.release()
        except Exception:
            logging.debug("Exception during capture release for %s", self.stream_link)
        finally:
            self._backend = "V4L2"


# ============================================================
# CAMERA DISCOVERY
# ============================================================


def _probe_once(cam_index: int) -> bool:
    """Open the device with V4L2 and pull one frame."""
    cap = cv2.VideoCapture(cam_index, cv2.CAP_V4L2)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return bool(cap.isOpened() and cap.grab())
    finally:
        _release_quietly(cap)


def _probe_with_retries(cam_index: int, retries: int, delay: float) -> bool:
    for _ in range(retries):
        if _probe_once(cam_index):
            return True
        time.sleep(delay)
    return False


def test_single_camera(
    cam_index: int,
    retries: int = 3,
    retry_delay: float = 0.2,
    allow_kill: bool = True,
    post_kill_retries: int = 2,
    post_kill_delay: float = 0.25,
) -> Optional[int]:
    """Return ``cam_index`` if the device delivers a frame, else None.

    Retries exist because a camera is briefly busy right after USB
    enumeration and right after another process releases it. When
    ``allow_kill`` is set and the config permits it, a device that still
    will not open gets its holder processes evicted and is probed again;
    that is how the dashboard reclaims a camera from a stale instance of
    itself after a crash.

    The name starts with ``test_`` for historical reasons; tests refer to it
    through the module so pytest does not collect it.
    """
    if _probe_with_retries(cam_index, retries, retry_delay):
        return cam_index
    if allow_kill and config.KILL_DEVICE_HOLDERS:
        if kill_device_holders(f"/dev/video{cam_index}"):
            if _probe_with_retries(cam_index, post_kill_retries, post_kill_delay):
                return cam_index
    return None


def get_video_indexes() -> list[int]:
    """Numeric indexes of every ``/dev/video*`` node, ascending."""
    indexes: list[int] = []
    for device in glob_module.glob("/dev/video*"):
        suffix = device.rsplit("video", 1)[-1]
        if suffix.isdigit():
            indexes.append(int(suffix))
        else:
            logging.debug("Skipping non-numeric video device: %s", device)
    return sorted(indexes)


def _probe_many(indexes: list[int], confirm_msg: str, **probe_kwargs) -> list[int]:
    """Probe several indexes concurrently; return those that passed."""
    working: list[int] = []
    with ThreadPoolExecutor(max_workers=min(4, len(indexes))) as executor:
        futures = {
            executor.submit(test_single_camera, idx, **probe_kwargs): idx
            for idx in indexes
        }
        for future in as_completed(futures):
            cam_idx = futures[future]
            try:
                result = future.result()
            except Exception:
                logging.exception("Exception testing camera %d", cam_idx)
                continue
            if result is not None:
                working.append(result)
                logging.info(confirm_msg, result)
    return working


def find_working_cameras() -> list[int]:
    """Return the sorted indexes of every camera that can capture frames."""
    indexes = get_video_indexes()
    if not indexes:
        logging.info("No /dev/video* devices found!")
        return []

    logging.info(
        "Testing %d cameras concurrently (workers=%d)...",
        len(indexes),
        min(4, len(indexes)),
    )
    working = _probe_many(indexes, "Camera %d OK")

    if working:
        logging.info("Round 2 - Double-check (no pre-kill)...")
        working = _probe_many(
            working,
            "Confirmed camera %d",
            retries=2,
            retry_delay=0.15,
            allow_kill=False,
        )

    working.sort()
    logging.info("FINAL Working cameras: %s", working)
    return working
