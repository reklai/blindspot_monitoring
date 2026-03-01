"""
Camera capture and discovery for Camera Dashboard.

Contains CaptureWorker for threaded video capture and
functions for discovering available cameras.
"""

from __future__ import annotations

import glob as glob_module
import logging
import platform
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
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

    def _open_capture(self) -> None:
        """Open the camera and apply preferred capture settings."""
        try:
            cap = None
            backend_name = "V4L2"

            def _try_v4l2_open(forced_fourcc: Optional[str]) -> Optional[cv2.VideoCapture]:
                backend = cv2.CAP_ANY
                if platform.system() == "Linux":
                    backend = cv2.CAP_V4L2
                local_cap = cv2.VideoCapture(self.stream_link, backend)
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

            # Try GStreamer first if enabled and available (more efficient MJPEG pipeline)
            if (
                config.USE_GSTREAMER
                and _check_gstreamer_available()
                and platform.system() == "Linux"
                and isinstance(self.stream_link, int)
            ):
                try:
                    w = int(self.capture_width) if self.capture_width else 640
                    h = int(self.capture_height) if self.capture_height else 480
                    # Use jpegdec (libjpeg) for MJPEG decoding - stable and efficient
                    # GStreamer pipeline optimized for low-latency:
                    # - v4l2src: capture from V4L2 device
                    # - queue: decouple source from decode (max 2 buffers, leaky=downstream)
                    # - appsink: sync=false for no A/V sync overhead, drop=1 for frame dropping
                    # - max-buffers=1: only keep latest frame to minimize latency
                    pipeline = (
                        f"v4l2src device=/dev/video{self.stream_link} ! "
                        f"image/jpeg,width={w},height={h} ! "
                        f"queue max-size-buffers=2 leaky=downstream ! "
                        f"jpegdec ! videoconvert ! "
                        f"appsink drop=1 max-buffers=1 sync=false"
                    )
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
    cam_index: int,
    retries: int = 3,
    retry_delay: float = 0.2,
    allow_kill: bool = True,
    post_kill_retries: int = 2,
    post_kill_delay: float = 0.25,
) -> Optional[int]:
    """Try to open and grab a frame from one camera index."""
    device_path = f"/dev/video{cam_index}"

    def try_open():
        cap = cv2.VideoCapture(cam_index, cv2.CAP_V4L2)
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
            return cam_index
        time.sleep(retry_delay)

    if allow_kill and config.KILL_DEVICE_HOLDERS:
        killed = kill_device_holders(device_path)
        if killed:
            for _ in range(post_kill_retries):
                if try_open():
                    return cam_index
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


def find_working_cameras() -> list[int]:
    """Return a list of camera indices that can capture frames."""
    indexes = get_video_indexes()
    if not indexes:
        logging.info("No /dev/video* devices found!")
        return []

    max_workers = min(4, len(indexes))
    logging.info(
        "Testing %d cameras concurrently (workers=%d)...", len(indexes), max_workers
    )
    working = []
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(test_single_camera, idx): idx for idx in indexes}
        for future in as_completed(futures):
            cam_idx = futures[future]
            try:
                result = future.result()
                if result is not None:
                    with lock:
                        working.append(result)
                        logging.info("Camera %d OK", result)
            except Exception:
                logging.exception("Exception testing camera %d", cam_idx)

    # Second pass to confirm cameras without killing holders
    if working:
        logging.info("Round 2 - Double-check (no pre-kill)...")
        final_working = []
        with ThreadPoolExecutor(max_workers=min(4, len(working))) as executor:
            futures = {
                executor.submit(
                    test_single_camera,
                    idx,
                    retries=2,
                    retry_delay=0.15,
                    allow_kill=False,
                ): idx
                for idx in working
            }
            for future in as_completed(futures):
                cam_idx = futures[future]
                try:
                    result = future.result()
                    if result is not None:
                        final_working.append(result)
                        logging.info("Confirmed camera %d", result)
                except Exception:
                    logging.exception("Exception confirming camera %d", cam_idx)
        working = final_working

    working = sorted(working)
    logging.info("FINAL Working cameras: %s", working)
    return working
