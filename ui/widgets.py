"""Dashboard tiles, fullscreen presentation, and UI-side capture recovery."""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Callable, Optional, Union

import cv2
import numpy as np
from numpy.typing import NDArray
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt, QTimer, pyqtSlot

from core import config
from core.camera import CaptureWorker


# Qt must retain a live QThread wrapper until its thread exits. Calling
# deleteLater(), or dropping the last Python reference, while it is running
# aborts the process. Workers that stop() cannot join are therefore held
# strongly here and deleted by a timer after exit. Their capture descriptors
# may remain open meanwhile; parking protects process lifetime, not device
# availability.
_zombie_workers: list[CaptureWorker] = []
_zombie_reap_timer: Optional[QTimer] = None
_ZOMBIE_REAP_INTERVAL_MS = 30_000


def _park_zombie_worker(worker: CaptureWorker) -> None:
    """Keep an unjoinable worker alive and ensure it is polled for exit."""
    global _zombie_reap_timer
    _zombie_workers.append(worker)
    if _zombie_reap_timer is None:
        _zombie_reap_timer = QTimer()
        _zombie_reap_timer.setInterval(_ZOMBIE_REAP_INTERVAL_MS)
        _zombie_reap_timer.timeout.connect(_reap_zombie_workers)
    if not _zombie_reap_timer.isActive():
        _zombie_reap_timer.start()


def _reap_zombie_workers() -> None:
    """Delete stopped parked workers and retain those that are still running."""
    still_running: list[CaptureWorker] = []
    for worker in _zombie_workers:
        try:
            if worker.isRunning():
                still_running.append(worker)
                continue
            worker.deleteLater()
        except RuntimeError:
            # Qt already destroyed the wrapped object; no deferred delete remains.
            pass
    _zombie_workers[:] = still_running
    if not still_running and _zombie_reap_timer is not None:
        _zombie_reap_timer.stop()


class FullscreenOverlay(QtWidgets.QWidget):
    """Frameless top-level window that presents one tile fullscreen."""

    def __init__(self, on_click_exit: Callable[[], None]) -> None:
        """Build the lazily created overlay and bind its exit callback."""
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.on_click_exit = on_click_exit
        self._touch_active = False
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.setStyleSheet("background:black;")
        self.label = QtWidgets.QLabel(self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setScaledContents(True)
        self.label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Ignored
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)

    def mousePressEvent(self, a0: QtGui.QMouseEvent) -> None:  # type: ignore[override]
        """Exit fullscreen for a left-button press."""
        if a0.button() == QtCore.Qt.MouseButton.LeftButton:
            self.on_click_exit()
        super().mousePressEvent(a0)

    def event(self, a0: QtCore.QEvent) -> bool:  # type: ignore[override]
        """Consume a touch sequence and invoke the exit callback once at its end."""
        if a0.type() == QtCore.QEvent.Type.TouchBegin:
            self._touch_active = True
            return True
        if a0.type() == QtCore.QEvent.Type.TouchEnd:
            if self._touch_active:
                self._touch_active = False
                self.on_click_exit()
            return True
        return super().event(a0)


class CameraWidget(QtWidgets.QWidget):
    """Dashboard tile for a camera, placeholder, or the shared settings controls."""

    # Minimum press duration used to select a tile for swapping.
    hold_threshold_ms: int = 400

    camera_stream_link: Optional[Union[int, str]]
    worker: Optional[CaptureWorker]
    _fs_overlay: Optional[FullscreenOverlay]

    def __init__(
        self,
        stream_link: Optional[Union[int, str]] = 0,
        parent: Optional[QtWidgets.QWidget] = None,
        target_fps: Optional[float] = None,
        request_capture_size: Optional[tuple[int, int]] = (640, 480),
        ui_fps: int = 15,
        enable_capture: bool = True,
        placeholder_text: Optional[str] = None,
        settings_mode: bool = False,
        on_restart: Optional[Callable[[], None]] = None,
        on_night_mode_toggle: Optional[Callable[[], None]] = None,
        on_brightness_change: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Build a camera, placeholder, or settings tile.

        Capture starts only when both ``enable_capture`` and ``stream_link`` are
        set. A settings tile replaces video presentation with callback-backed
        controls but still participates in the grid's swap behavior.
        """
        super().__init__(parent)
        logging.debug("Creating camera %s", stream_link)

        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.setMouseTracking(True)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )

        self.camera_stream_link = stream_link
        # Object names need per-instance uniqueness; retaining the path tail
        # keeps logs readable without conflating two tiles for the same stream.
        id_label = stream_link[-24:] if isinstance(stream_link, str) else stream_link
        self.widget_id = f"cam{id_label}_{id(self)}"

        # main.py assigns one stable, unique index per camera slot. Rescans
        # reuse the same tile object; -1 means it has not been placed yet.
        self.slot_index: int = -1

        self.is_fullscreen = False
        self.grid_position = None
        self._press_widget_id = None
        self._press_time = 0
        self._grid_parent = None
        self._touch_active = False
        self.swap_active = False
        self._last_fullscreen_toggle_ts = 0.0
        # Suppress duplicate rapid input, including mouse events synthesized from touch.
        self._fullscreen_debounce_ms = 200

        self._fs_overlay = None

        self.capture_enabled = bool(enable_capture)
        self.placeholder_text = placeholder_text
        self.settings_mode = settings_mode
        self.night_mode_enabled = False
        self.brightness = 1.0  # Rendering multiplier; 1.0 leaves pixels unchanged.

        self.normal_style = "background: black;"
        self.swap_ready_style = "border: 6px solid #FFFF00; background: black;"
        self.setStyleSheet(self.normal_style)
        self.setObjectName(self.widget_id)

        # The label fills the tile and receives pointer events, so it installs
        # the same gesture handling as the parent widget.
        self.video_label = QtWidgets.QLabel(self)
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setScaledContents(True)
        self.video_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.video_label.setMinimumSize(1, 1)
        self.video_label.setMouseTracking(True)
        self.video_label.setObjectName(f"{self.widget_id}_label")
        self.video_label.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_AcceptTouchEvents, True
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._layout = layout  # Swap selection changes these margins.

        if self.settings_mode:
            self.video_label.setText("")
            self.video_label.setFixedSize(0, 0)

            # QLabel controls provide large touch targets; eventFilter dispatches
            # their callbacks by object name.
            btn_style = "QLabel { padding: 8px 12px; margin: 2px; background: #333; color: white; border-radius: 4px; }"

            self._label_buttons = {}

            def add_setting_button(text: str, callback):
                label = QtWidgets.QLabel(text)
                label.setStyleSheet(btn_style)
                label.setAttribute(QtCore.Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
                label.installEventFilter(self)
                label.setObjectName(f"btn_{text}")
                self._label_buttons[label.objectName()] = callback
                return label

            restart_label = add_setting_button("Restart", on_restart)
            night_mode_label = add_setting_button("Nightmode: Off", on_night_mode_toggle)
            self.night_mode_button = night_mode_label

            brightness_layout = QtWidgets.QHBoxLayout()
            brightness_layout.setSpacing(4)
            self._brightness_buttons = {}
            brightness_values = [15, 60, 80, 100, 150]
            brightness_labels = ["15%", "60%", "80%", "100%", "150%"]

            # The outer callback keeps every camera tile synchronized.
            self._on_brightness_change = on_brightness_change

            def brightness_callback(v):
                self._set_brightness_value(v)
                if self._on_brightness_change:
                    self._on_brightness_change(v)

            for val, label in zip(brightness_values, brightness_labels):
                btn = QtWidgets.QLabel(label)
                btn.setStyleSheet(btn_style)
                btn.setAttribute(QtCore.Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
                btn.installEventFilter(self)
                btn.setObjectName(f"brightness_{val}")
                self._brightness_buttons[val] = btn
                # Bind val now; eventFilter invokes the callback after this loop.
                self._label_buttons[btn.objectName()] = lambda v=val, cb=brightness_callback: cb(v)
                brightness_layout.addWidget(btn)

            self._current_brightness = 100

            brightness_label = QtWidgets.QLabel("Brightness")
            brightness_label.setStyleSheet("color: white; padding: 4px; font-weight: bold;")
            brightness_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

            left_layout = QtWidgets.QVBoxLayout()
            left_layout.addWidget(restart_label, alignment=Qt.AlignmentFlag.AlignCenter)
            left_layout.addSpacing(8)
            left_layout.addWidget(night_mode_label, alignment=Qt.AlignmentFlag.AlignCenter)
            left_layout.addSpacing(8)
            left_layout.addWidget(brightness_label, alignment=Qt.AlignmentFlag.AlignCenter)
            brightness_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            left_layout.addLayout(brightness_layout)

            main_layout = QtWidgets.QHBoxLayout()
            main_layout.addStretch(1)
            main_layout.addLayout(left_layout, stretch=1)
            main_layout.addStretch(1)

            layout.addStretch(1)
            layout.addLayout(main_layout)
            layout.addStretch(1)
        else:
            layout.addWidget(self.video_label)

        # Frame freshness, restart-budget state, and render caches.
        self.frame_count = 0
        self.prev_time = time.time()
        self._latest_frame = None
        self._last_placeholder_text = None
        self._last_placeholder_fullscreen = None
        self._frame_id = 0
        self._last_rendered_id = -1
        self._last_rendered_size = None
        self._last_frame_ts = 0.0
        self._stale_frame_timeout_sec = config.STALE_FRAME_TIMEOUT_SEC
        self._restart_cooldown_sec = config.RESTART_COOLDOWN_SEC
        self._restart_window_sec = config.RESTART_WINDOW_SEC
        self._max_restarts_per_window = config.MAX_RESTARTS_PER_WINDOW
        self._restart_events = deque(maxlen=config.MAX_RESTARTS_PER_WINDOW * 2)
        self._last_restart_ts = 0.0
        self._restart_limit_logged = False
        # This timestamp starts a first-frame watchdog for each worker lifetime.
        self._attach_ts = 0.0
        self._first_frame_timeout_sec = config.FIRST_FRAME_TIMEOUT_SEC
        # Keep this separate from _last_frame_ts: an online status refreshes
        # that timestamp even when the first grab/retrieve never completes.
        self._frame_since_attach = False
        # An unjoinable worker cannot advance the restart budget once self.worker
        # is cleared, so this flag independently lets rescan reclaim the slot.
        self._leaked_worker = False
        self._last_status_log_ts = 0.0
        self._last_status_log_interval_sec = 10.0
        self._pixmap_cache = QtGui.QPixmap()
        self._scaled_pixmap_cache = None
        self._scaled_pixmap_cache_size = None
        self._night_gray = None
        self._night_bgr = None
        # Brightness uses a separate, per-resolution buffer so a size-triggered
        # re-render never applies the LUT twice to _latest_frame.
        self._brightness_buffer = None
        self._night_lut = np.clip(np.arange(256, dtype=np.float32) * 1.6, 0, 255).astype(np.uint8)
        self._brightness_lut = np.arange(256, dtype=np.uint8)

        # Stress control changes the live software-emission/UI targets and later
        # recovers them toward their configured baselines.
        self.base_target_fps = target_fps
        self.current_target_fps = target_fps

        # Establish the target UI cadence before spawning; the same value is the
        # worker's emission ceiling. Settings tiles do not render frames.
        if not self.settings_mode:
            self.ui_render_fps = max(1, int(ui_fps))
            self.base_ui_fps = self.ui_render_fps
        else:
            self.ui_render_fps = 0
            self.base_ui_fps = 0

        self.worker = None
        if self.capture_enabled and stream_link is not None:
            self._spawn_worker(stream_link, target_fps, request_capture_size)
            self._attach_ts = time.time()
        elif not self.settings_mode:
            self._latest_frame = None
            self._render_placeholder(self.placeholder_text or "DISCONNECTED")

        # The GUI timer samples only the newest frame at the target cadence.
        if not self.settings_mode:
            interval = max(1, int(1000 / self.ui_render_fps) - config.RENDER_OVERHEAD_MS)
            self.render_timer = QTimer(self)
            self.render_timer.setInterval(interval)
            self.render_timer.timeout.connect(self._render_latest_frame)
            self.render_timer.start()
        else:
            self.render_timer = None

        if self.capture_enabled and not self.settings_mode and config.UI_FPS_LOGGING:
            self.ui_timer = QTimer(self)
            self.ui_timer.setInterval(1000)
            self.ui_timer.timeout.connect(self._print_fps)
            self.ui_timer.start()
        else:
            self.ui_timer = None

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(5000)
        self._status_timer.timeout.connect(self._log_status)
        self._status_timer.start()

        self.installEventFilter(self)
        self.video_label.installEventFilter(self)

        logging.debug("Widget %s ready", self.widget_id)

    def _exit_app(self) -> None:
        """Request event-loop shutdown when a QApplication exists."""
        app = QtWidgets.QApplication.instance()
        if app:
            app.quit()

    def _ensure_fullscreen_overlay(self) -> None:
        """Create the top-level overlay lazily instead of one per tile at startup."""
        if self._fs_overlay is None:
            self._fs_overlay = FullscreenOverlay(self.exit_fullscreen)

    def _apply_ui_fps(self, ui_fps: int) -> None:
        """Set the target UI cadence and the worker's matching emission ceiling.

        The timer subtracts configured render overhead to approximate the
        requested paint rate; actual painting still depends on GUI workload.
        The worker receives the numeric target as a strict emission bound.
        """
        self.ui_render_fps = max(1, int(ui_fps))
        if self.render_timer:
            interval = max(1, int(1000 / self.ui_render_fps) - config.RENDER_OVERHEAD_MS)
            self.render_timer.setInterval(interval)
        if self.worker is not None:
            self.worker.set_ui_fps(self.ui_render_fps)

    def _spawn_worker(
        self,
        stream_link: Union[int, str],
        target_fps: Optional[float],
        capture_size: Optional[tuple[int, int]],
    ) -> None:
        """Create, connect, and start the tile's capture worker.

        ``target_fps`` requests the V4L2 device rate and seeds the software
        emission throttle for either capture backend. ``ui_render_fps`` is a
        separate emission ceiling aligned with this tile's target UI cadence.
        """
        cap_w, cap_h = capture_size if capture_size else (None, None)
        self.worker = CaptureWorker(
            stream_link,
            parent=self,
            target_fps=target_fps,
            capture_width=cap_w,
            capture_height=cap_h,
            ui_fps=self.ui_render_fps,
        )
        self.worker.frame_ready.connect(self.on_frame)
        self.worker.status_changed.connect(self.on_status_changed)
        self.worker.start()

    def attach_camera(
        self,
        stream_link: Union[int, str],
        target_fps: float,
        request_capture_size: tuple[int, int],
        ui_fps: Optional[int] = None,
    ) -> None:
        """Convert this placeholder into an active camera without changing its slot."""
        if self.capture_enabled and self.worker:
            return

        self._restart_events.clear()
        self._restart_limit_logged = False
        self._leaked_worker = False
        self._last_restart_ts = 0.0

        self.capture_enabled = True
        self.camera_stream_link = stream_link
        self.base_target_fps = target_fps
        self.current_target_fps = target_fps

        if ui_fps is not None:
            self._apply_ui_fps(ui_fps)
            self.base_ui_fps = max(1, int(ui_fps))

        self._spawn_worker(stream_link, target_fps, request_capture_size)
        self._attach_ts = time.time()
        self._frame_since_attach = False

        if self.ui_timer is None and config.UI_FPS_LOGGING:
            self.ui_timer = QTimer(self)
            self.ui_timer.setInterval(1000)
            self.ui_timer.timeout.connect(self._print_fps)
            self.ui_timer.start()

        self._latest_frame = None
        self._render_placeholder("CONNECTING...")
        logging.info("Attached camera %s to widget %s", stream_link, self.widget_id)

    def eventFilter(self, a0: QtCore.QObject, a1: QtCore.QEvent) -> bool:  # type: ignore[override]
        """Normalize settings-control and tile input into one gesture path."""
        if self.settings_mode and isinstance(a0, QtWidgets.QLabel):
            obj_name = a0.objectName()
            if obj_name in self._label_buttons:
                if a1.type() == QtCore.QEvent.Type.TouchBegin:
                    self._touch_active = True
                    self._press_time = time.time() * 1000.0
                    return True
                if a1.type() == QtCore.QEvent.Type.TouchEnd:
                    if self._touch_active:
                        self._touch_active = False
                        callback = self._label_buttons.get(obj_name)
                        if callback:
                            callback()
                    return True
                if a1.type() == QtCore.QEvent.Type.MouseButtonPress:
                    return True
                if a1.type() == QtCore.QEvent.Type.MouseButtonRelease:
                    callback = self._label_buttons.get(obj_name)
                    if callback:
                        callback()
                    return True

        # Preserve native handling if settings controls return to QPushButton.
        if isinstance(a0, QtWidgets.QPushButton):
            return super().eventFilter(a0, a1)

        if a0 not in (self, self.video_label) or a1 is None:
            return super().eventFilter(a0, a1)

        if a1.type() == QtCore.QEvent.Type.TouchBegin:
            return self._on_touch_begin(a1)
        if a1.type() == QtCore.QEvent.Type.TouchEnd:
            return self._on_touch_end(a1)

        if a1.type() == QtCore.QEvent.Type.MouseButtonPress:
            return self._on_mouse_press(a1)
        if a1.type() == QtCore.QEvent.Type.MouseButtonRelease:
            return self._on_mouse_release(a1)
        return super().eventFilter(a0, a1)

    def _on_touch_begin(self, event: Any) -> bool:
        """Begin a single-touch gesture, consuming multitouch without state changes."""
        try:
            if not event.points():
                return True
            if len(event.points()) == 1:
                self._touch_active = True
                self._press_time = time.time() * 1000.0
                self._press_widget_id = self.widget_id
                self._grid_parent = self.parent()
                logging.debug("Touch begin %s", self.widget_id)
        except Exception:
            logging.exception("touch begin")
        return True

    def _on_touch_end(self, event: Any) -> bool:
        """Resolve an active touch through the same release path as the mouse."""
        try:
            if not self._touch_active:
                return True
            self._touch_active = False
            self._handle_release_as_left_click()
        except Exception:
            logging.exception("touch end")
        return True

    def _handle_release_as_left_click(self) -> bool:
        """Resolve a completed primary-pointer gesture.

        On grid parents that expose ``selected_camera``, an existing selection
        takes priority: releasing it cancels, and releasing another tile swaps.
        Otherwise a long press selects this tile and a short press toggles
        fullscreen. Settings tiles remain swappable but ignore that short-release
        action; right-click uses a separate immediate fullscreen path for every
        tile.
        """
        try:
            if not self._press_widget_id or self._press_widget_id != self.widget_id:
                return True

            hold_time = (time.time() * 1000.0) - self._press_time
            logging.debug("Release %s hold=%dms", self.widget_id, int(hold_time))

            swap_parent = self._grid_parent
            if not swap_parent or not hasattr(swap_parent, "selected_camera"):
                if self.settings_mode:
                    self._reset_mouse_state()
                    return True
                self._reset_mouse_state()
                self.toggle_fullscreen()
                return True

            selected = getattr(swap_parent, "selected_camera", None)

            if selected == self:
                logging.debug("Clear swap %s", self.widget_id)
                setattr(swap_parent, "selected_camera", None)
                self.swap_active = False
                self.reset_style()
                self._reset_mouse_state()
                return True

            if selected and selected != self and not self.is_fullscreen:
                other = selected
                logging.debug("SWAP %s <-> %s", other.widget_id, self.widget_id)
                self.do_swap(other, self, swap_parent)
                other.swap_active = False
                other.reset_style()
                setattr(swap_parent, "selected_camera", None)
                self._reset_mouse_state()
                return True

            if hold_time >= self.hold_threshold_ms and not self.is_fullscreen:
                logging.debug("ENTER swap %s", self.widget_id)
                setattr(swap_parent, "selected_camera", self)
                self.swap_active = True
                # Inset the zero-margin content so the selection border is visible.
                self._layout.setContentsMargins(6, 6, 6, 6)
                self.setStyleSheet(self.swap_ready_style)
                self._reset_mouse_state()
                return True

            if self.settings_mode:
                self._reset_mouse_state()
                return True

            logging.debug("Short tap fullscreen %s", self.widget_id)
            self.toggle_fullscreen()

        except Exception:
            logging.exception("touch release")
        finally:
            self._reset_mouse_state()
        return True

    def _on_mouse_press(self, event: Any) -> bool:
        """Begin a primary-button gesture; right-click toggles fullscreen immediately."""
        try:
            if event.button() == QtCore.Qt.MouseButton.LeftButton:
                self._press_time = time.time() * 1000.0
                self._press_widget_id = self.widget_id
                self._grid_parent = self.parent()
                logging.debug("Press %s", self.widget_id)
            elif event.button() == QtCore.Qt.MouseButton.RightButton:
                self.toggle_fullscreen()
        except Exception:
            logging.exception("mouse press")
        return True

    def _on_mouse_release(self, event: Any) -> bool:
        """Resolve a primary-button release through the shared gesture path."""
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return True
        return self._handle_release_as_left_click()

    def _reset_mouse_state(self) -> None:
        """Clear the gesture origin so an unrelated release cannot reuse it."""
        self._press_time = 0
        self._press_widget_id = None
        self._grid_parent = None

    def do_swap(
        self,
        source: CameraWidget,
        target: CameraWidget,
        layout_parent: Any,
    ) -> None:
        """Exchange two tiles' layout cells and their cached grid positions."""
        try:
            source_pos = getattr(source, "grid_position", None)
            target_pos = getattr(target, "grid_position", None)
            if source_pos is None or target_pos is None:
                logging.debug("Swap failed - missing positions")
                return

            layout = layout_parent.layout()
            layout.removeWidget(source)
            layout.removeWidget(target)
            layout.addWidget(target, *source_pos)
            layout.addWidget(source, *target_pos)
            source.grid_position, target.grid_position = target_pos, source_pos
            logging.debug("Swap complete %s <-> %s", source.widget_id, target.widget_id)
        except Exception:
            logging.exception("do_swap")

    def toggle_fullscreen(self) -> None:
        """Toggle the overlay, ignoring duplicate events inside the debounce window."""
        now = time.time() * 1000.0
        if (now - self._last_fullscreen_toggle_ts) < self._fullscreen_debounce_ms:
            logging.debug("Fullscreen toggle debounced for %s", self.widget_id)
            return
        self._last_fullscreen_toggle_ts = now

        if self.is_fullscreen:
            self.exit_fullscreen()
        else:
            self.go_fullscreen()

    def go_fullscreen(self) -> None:
        """Present this tile on the primary screen's fullscreen overlay."""
        if self.is_fullscreen:
            return
        self._ensure_fullscreen_overlay()

        if self._fs_overlay is None:
            return

        screen = QtWidgets.QApplication.primaryScreen()
        if screen:
            self._fs_overlay.setGeometry(screen.geometry())

        self._fs_overlay.showFullScreen()
        self._fs_overlay.raise_()
        self._fs_overlay.activateWindow()
        self.is_fullscreen = True

        if self._latest_frame is None and not self.settings_mode:
            self._render_placeholder(self.placeholder_text or "DISCONNECTED")

    def exit_fullscreen(self) -> None:
        """Hide this tile's overlay and resume grid presentation."""
        if not self.is_fullscreen:
            return
        if self._fs_overlay:
            self._fs_overlay.hide()
        self.is_fullscreen = False

    @pyqtSlot(object)
    def on_frame(self, frame_bgr: NDArray[np.uint8]) -> None:
        """Retain the newest worker frame for the GUI render timer.

        Each queued signal carries a fresh private array, so retaining the
        reference is safe and avoids another full-frame copy.
        """
        try:
            if frame_bgr is None:
                return
            self._latest_frame = frame_bgr
            self._frame_id += 1
            self._last_frame_ts = time.time()
            self._frame_since_attach = True
        except Exception:
            logging.exception("on_frame")

    def _release_current_frame(self) -> None:
        """Drop the retained frame so the next render shows placeholder state."""
        self._latest_frame = None

    def _dispose_worker(self, worker: CaptureWorker) -> None:
        """Disconnect and release a worker without destroying a live QThread.

        A stopped worker follows Qt's normal deferred-deletion path. A running
        worker is parked under a strong reference until the reaper observes
        that its thread has exited.
        """
        try:
            worker.frame_ready.disconnect(self.on_frame)
        except Exception:
            pass
        try:
            worker.status_changed.disconnect(self.on_status_changed)
        except Exception:
            pass
        try:
            worker.setParent(None)
            if worker.isRunning():
                _park_zombie_worker(worker)
                return
            worker.deleteLater()
        except Exception:
            pass

    def _render_placeholder(self, text: str) -> None:
        """Show text on the active label, skipping identical QLabel updates."""
        if self.settings_mode:
            return
        if (
            text == self._last_placeholder_text
            and not self.swap_active
            and self.is_fullscreen == self._last_placeholder_fullscreen
        ):
            return
        target_label = (
            self._fs_overlay.label
            if (self.is_fullscreen and self._fs_overlay)
            else self.video_label
        )
        target_label.setPixmap(QtGui.QPixmap())
        target_label.setText(text)
        target_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        target_label.setStyleSheet("color: #bbbbbb; font-size: 24px;")
        self._last_placeholder_text = text
        self._last_placeholder_fullscreen = self.is_fullscreen
        if self.swap_active:
            self.setStyleSheet(self.swap_ready_style)

    def _blit_scaled(
        self,
        target_label: QtWidgets.QLabel,
        target_size: QtCore.QSize,
        needs_scale: bool,
    ) -> None:
        """Copy the frame pixmap to a label, reusing the target-sized buffer.

        ``needs_scale`` selects an explicit paint into the cached black pixmap;
        otherwise the source pixmap can be assigned directly.
        """
        if needs_scale:
            if (
                self._scaled_pixmap_cache is None
                or self._scaled_pixmap_cache_size != target_size
            ):
                self._scaled_pixmap_cache = QtGui.QPixmap(target_size)
                self._scaled_pixmap_cache_size = target_size
            self._scaled_pixmap_cache.fill(Qt.GlobalColor.black)
            target_rect = QtCore.QRect(
                0, 0, target_size.width(), target_size.height()
            )
            painter = QtGui.QPainter(self._scaled_pixmap_cache)
            painter.drawPixmap(target_rect, self._pixmap_cache)
            painter.end()
            target_label.setPixmap(self._scaled_pixmap_cache)
        else:
            target_label.setPixmap(self._pixmap_cache)
        target_label.setText("")

    def _render_latest_frame(self) -> None:
        """Render when the frame or target size changes and enforce stale recovery."""
        if self.settings_mode:
            return
        try:
            frame_bgr = self._latest_frame
            if frame_bgr is None:
                self._render_placeholder(self.placeholder_text or "DISCONNECTED")
                # The first-frame flag distinguishes an initial read wedge from
                # a mid-run disconnect. The worker owns mid-run reconnection,
                # while an initial grab can block before its loop can recover.
                # _last_frame_ts cannot make that distinction because an online
                # status refreshes it before any frame is delivered.
                if (
                    self.capture_enabled
                    and self.worker is not None
                    and not self._frame_since_attach
                    and (time.time() - self._attach_ts) > self._first_frame_timeout_sec
                ):
                    self._restart_capture_if_stale()
                return

            if (
                self._last_frame_ts
                and (time.time() - self._last_frame_ts) > self._stale_frame_timeout_sec
            ):
                stale_duration = time.time() - self._last_frame_ts
                logging.warning(
                    "Camera %s: Stale frame detected (no frames for %.1fs)",
                    self.camera_stream_link,
                    stale_duration,
                )
                self._release_current_frame()
                self._last_rendered_id = -1
                self._render_placeholder("DISCONNECTED")
                self._restart_capture_if_stale()
                return

            if self.is_fullscreen and self._fs_overlay:
                target_size = self._fs_overlay.size()
            else:
                target_size = self.video_label.size()

            if (
                self._frame_id == self._last_rendered_id
                and self._last_rendered_size == target_size
            ):
                return

            if self.night_mode_enabled:
                try:
                    if frame_bgr.ndim == 2:
                        h, w = frame_bgr.shape
                    else:
                        h, w = frame_bgr.shape[:2]

                    # Reuse contiguous transform buffers until resolution changes.
                    if self._night_gray is None or self._night_gray.shape != (h, w):
                        self._night_gray = np.empty((h, w), dtype=np.uint8)
                    if self._night_bgr is None or self._night_bgr.shape[:2] != (h, w):
                        self._night_bgr = np.zeros((h, w, 3), dtype=np.uint8, order='C')

                    if frame_bgr.ndim == 2:
                        cv2.LUT(frame_bgr, self._night_lut, dst=self._night_gray)
                    else:
                        cv2.cvtColor(
                            frame_bgr, cv2.COLOR_BGR2GRAY, dst=self._night_gray
                        )
                        cv2.LUT(self._night_gray, self._night_lut, dst=self._night_gray)

                    # Night mode is red-only; B/G remain zero in this buffer.
                    self._night_bgr[:, :, 2] = self._night_gray
                    frame_bgr = self._night_bgr
                except Exception:
                    logging.debug("Night mode processing failed", exc_info=True)

            # A whole-array LUT handles every channel into a separate reusable
            # buffer, preserving _latest_frame across size-triggered re-renders.
            if self.brightness != 1.0:
                try:
                    if (
                        self._brightness_buffer is None
                        or self._brightness_buffer.shape != frame_bgr.shape
                    ):
                        self._brightness_buffer = np.empty_like(frame_bgr, order='C')
                    cv2.LUT(frame_bgr, self._brightness_lut, dst=self._brightness_buffer)
                    frame_bgr = self._brightness_buffer
                except Exception:
                    logging.debug("Brightness processing failed", exc_info=True)

            # QImage wraps the numpy memory directly, so rows must be contiguous.
            if not frame_bgr.flags['C_CONTIGUOUS']:
                frame_bgr = np.ascontiguousarray(frame_bgr)

            if frame_bgr.ndim == 2:
                h, w = frame_bgr.shape[:2]
                bytes_per_line = w
                img = QtGui.QImage(
                    frame_bgr.data,
                    w,
                    h,
                    bytes_per_line,
                    QtGui.QImage.Format.Format_Grayscale8,
                )
            else:
                h, w = frame_bgr.shape[:2]
                ch = frame_bgr.shape[2] if frame_bgr.ndim > 2 else 1
                bytes_per_line = ch * w
                img = QtGui.QImage(
                    frame_bgr.data,
                    w,
                    h,
                    bytes_per_line,
                    QtGui.QImage.Format.Format_BGR888,
                )

            self._pixmap_cache.convertFromImage(img)

            # Fullscreen always fills the overlay; grid skips work at native size.
            if self.is_fullscreen and self._fs_overlay:
                needs_scale = target_size.width() > 0 and target_size.height() > 0
                self._blit_scaled(self._fs_overlay.label, target_size, needs_scale)
            else:
                needs_scale = (
                    target_size.width() > 0
                    and target_size.height() > 0
                    and self._pixmap_cache.size() != target_size
                )
                self._blit_scaled(self.video_label, target_size, needs_scale)

            self._last_rendered_id = self._frame_id
            self._last_rendered_size = target_size
            self._last_placeholder_text = None
            self._last_placeholder_fullscreen = None
            if config.UI_FPS_LOGGING:
                self.frame_count += 1
        except Exception:
            logging.exception("render frame")

    @pyqtSlot(bool)
    def on_status_changed(self, online: bool) -> None:
        """Reflect worker connectivity without treating online as a received frame.

        The online timestamp gives normal stale detection a grace period.
        ``_frame_since_attach`` remains the separate first-frame authority.
        """
        if online:
            self.setStyleSheet(
                self.swap_ready_style if self.swap_active else self.normal_style
            )
            self.video_label.setText("")
            self._last_frame_ts = time.time()
        else:
            self._release_current_frame()
            self._last_rendered_id = -1
            self._render_placeholder("DISCONNECTED")

    def reset_style(self) -> None:
        """Apply margins and border for the tile's current swap-selection state."""
        self.video_label.setStyleSheet("")
        if self.swap_active:
            self._layout.setContentsMargins(6, 6, 6, 6)
            self.setStyleSheet(self.swap_ready_style)
        else:
            self._layout.setContentsMargins(2, 2, 2, 2)
            self.setStyleSheet(self.normal_style)

    def _print_fps(self) -> None:
        """Log this tile's measured render rate when diagnostics are enabled."""
        if not config.UI_FPS_LOGGING:
            return
        try:
            now = time.time()
            elapsed = now - self.prev_time
            if elapsed >= 1.0:
                fps = self.frame_count / elapsed if elapsed > 0 else 0.0
                logging.info("%s FPS: %.1f", self.widget_id, fps)
                self.frame_count = 0
                self.prev_time = now
        except Exception:
            logging.debug("FPS logging exception", exc_info=True)

    def set_dynamic_fps(self, fps: Optional[float]) -> None:
        """Apply the stress monitor's software-emission target, clamped to its floor."""
        if fps is None or not self.capture_enabled:
            return
        try:
            fps = float(fps)
            if fps < config.MIN_DYNAMIC_FPS:
                fps = config.MIN_DYNAMIC_FPS
            self.current_target_fps = fps
            if self.worker:
                self.worker.set_target_fps(fps)
        except Exception:
            logging.exception("set_dynamic_fps")

    def set_dynamic_ui_fps(self, ui_fps: int) -> None:
        """Apply a stress-driven UI target and update the worker's emission ceiling."""
        if self.settings_mode:
            return
        try:
            ui_fps = int(ui_fps)
            if ui_fps < config.MIN_DYNAMIC_UI_FPS:
                ui_fps = config.MIN_DYNAMIC_UI_FPS
            self._apply_ui_fps(ui_fps)
        except Exception:
            logging.exception("set_dynamic_ui_fps")

    @property
    def _extended_cooldown_sec(self) -> float:
        """Return the long backoff shared by budget reset and detach eligibility."""
        return self._restart_window_sec * 2

    def is_permanently_failed(self, now: float) -> bool:
        """Return whether rescan may detach this tile after capture failure.

        Exhausting the restart budget or parking an unjoinable worker makes the
        tile eligible. Both cases wait through the extended cooldown so the
        slot is not reclaimed immediately after the last attempt.
        """
        if not (self._restart_limit_logged or self._leaked_worker):
            return False
        return (now - self._last_restart_ts) >= self._extended_cooldown_sec

    def _restart_capture_if_stale(self) -> None:
        """Try to replace a nonproducing worker within restart and cooldown limits."""
        if not self.capture_enabled or not self.worker:
            return
        now = time.time()
        if (now - self._last_restart_ts) < self._restart_cooldown_sec:
            return
        recent = [
            t for t in self._restart_events if (now - t) <= self._restart_window_sec
        ]
        if len(recent) >= self._max_restarts_per_window:
            # Pause before resetting the budget rather than abandon recovery.
            extended_cooldown = self._extended_cooldown_sec
            if (now - self._last_restart_ts) < extended_cooldown:
                if not getattr(self, '_restart_limit_logged', False):
                    logging.warning(
                        "Restart limit reached for %s, will retry in %.0fs",
                        self.camera_stream_link,
                        extended_cooldown
                    )
                    self._restart_limit_logged = True
                return
            logging.info(
                "Extended cooldown passed for %s, attempting recovery",
                self.camera_stream_link
            )
            self._restart_events.clear()
            self._restart_limit_logged = False

        # Stamp before stop(): even an unjoinable worker must not be retried on
        # every render tick.
        self._last_restart_ts = now

        old_worker = self.worker
        cap_w = getattr(old_worker, "capture_width", None)
        cap_h = getattr(old_worker, "capture_height", None)
        target_fps = self.current_target_fps or self.base_target_fps

        logging.info(
            "Restarting capture for %s after stale frames", self.camera_stream_link
        )

        # Never spawn a replacement while the old worker may still own the device.
        stopped = False
        try:
            stopped = old_worker.stop()
        except Exception:
            logging.exception("Error stopping old worker for %s", self.camera_stream_link)

        if not stopped:
            # A failed stop is not a completed restart, so it does not consume
            # budget; the timestamp above still enforces normal backoff.
            logging.error(
                "Old worker for %s could not be stopped; disposing zombie and "
                "leaving slot for rescan/detach",
                self.camera_stream_link,
            )
            self._dispose_worker(old_worker)
            self.worker = None
            # With no current worker, no later render can advance the budget;
            # the leak flag gives the detach sweep an independent trigger.
            self._leaked_worker = True
            self.on_status_changed(False)
            return

        # Only attempts that stopped the old worker consume restart budget.
        self._restart_events.append(now)

        self._dispose_worker(old_worker)

        # Retain a guard for partial teardown despite the capture-enabled invariant.
        if self.camera_stream_link is None:
            return

        self._spawn_worker(self.camera_stream_link, target_fps, (cap_w, cap_h))
        self._attach_ts = time.time()
        self._frame_since_attach = False
        self._render_placeholder("CONNECTING...")

    def _log_status(self) -> None:
        """Log periodic per-tile capture and render state."""
        if self.settings_mode:
            return
        if self.camera_stream_link is None:
            return
        now = time.time()
        if (now - self._last_status_log_ts) < self._last_status_log_interval_sec:
            return
        self._last_status_log_ts = now
        format_fourcc = "unknown"
        if self.worker is not None:
            format_fourcc = self.worker.get_fourcc()
        logging.info(
            "Camera %s status online=%s fps=%.1f ui_fps=%d fourcc=%s",
            self.camera_stream_link,
            "yes" if self._latest_frame is not None else "no",
            float(self.current_target_fps or 0),
            int(self.ui_render_fps or 0),
            format_fourcc,
        )

    def set_night_mode(self, enabled: bool) -> None:
        """Select normal color or red-only low-light rendering."""
        self.night_mode_enabled = bool(enabled)

    def set_night_mode_button_label(self, enabled: bool) -> None:
        """Keep the settings label synchronized with the shared night-mode state."""
        if self.settings_mode and hasattr(self, "night_mode_button"):
            label = "Nightmode: On" if enabled else "Nightmode: Off"
            self.night_mode_button.setText(label)

    def set_brightness(self, value: float) -> None:
        """Set the render multiplier, clamped to 0.5–3.0, and rebuild its LUT."""
        self.brightness = max(0.5, min(3.0, value))
        # Precompute once per setting change instead of multiplying every frame.
        input_vals = np.arange(256, dtype=np.float32)
        if self.brightness < 1.0:
            max_out = 255 * self.brightness
            self._brightness_lut = (input_vals * (max_out / 255.0)).astype(np.uint8)
        else:
            self._brightness_lut = np.clip(input_vals * self.brightness, 0, 255).astype(np.uint8)
        self._update_brightness_buttons()

    def _set_brightness_value(self, value: int) -> None:
        """Translate a settings percentage; ``set_brightness`` applies its clamp."""
        self._current_brightness = value
        brightness_factor = value / 100.0
        self.set_brightness(brightness_factor)
        self._update_brightness_buttons()

    def _update_brightness_buttons(self) -> None:
        """Highlight the settings preset matching ``_current_brightness``."""
        if not hasattr(self, '_brightness_buttons'):
            return
        btn_style = "QLabel { padding: 8px 12px; margin: 2px; background: #333; color: white; border-radius: 4px; }"
        selected_style = "QLabel { padding: 8px 12px; margin: 2px; background: #666; color: white; border-radius: 4px; font-weight: bold; }"
        for val, btn in self._brightness_buttons.items():
            btn.setStyleSheet(selected_style if val == self._current_brightness else btn_style)

    def cleanup(self) -> None:
        """Best-effort stop timers, dispose the worker, and delete the overlay."""
        try:
            if self.render_timer is not None and self.render_timer.isActive():
                self.render_timer.stop()
            if self.ui_timer is not None and self.ui_timer.isActive():
                self.ui_timer.stop()
            if self._status_timer is not None and self._status_timer.isActive():
                self._status_timer.stop()

            worker = self.worker if hasattr(self, "worker") else None
            if worker:
                try:
                    worker.frame_ready.disconnect(self.on_frame)
                except Exception:
                    pass
                try:
                    worker.status_changed.disconnect(self.on_status_changed)
                except Exception:
                    pass
                try:
                    worker.stop()
                except Exception:
                    logging.debug("Error stopping worker during cleanup", exc_info=True)
                self._release_current_frame()
                self._dispose_worker(worker)
                self.worker = None

            if self._fs_overlay is not None:
                try:
                    self._fs_overlay.hide()
                    self._fs_overlay.setParent(None)
                    self._fs_overlay.deleteLater()
                except Exception:
                    pass
                self._fs_overlay = None
                self.is_fullscreen = False
        except Exception:
            pass

    def detach_camera(self) -> Optional[Union[int, str]]:
        """Convert an active camera tile back into a reusable placeholder.

        Returns the previous stream link as a success value for status logging.
        Placeholder and settings tiles return ``None`` without changing state.
        """
        if not self.capture_enabled or self.settings_mode:
            return None
        
        detached_index = self.camera_stream_link
        
        worker = self.worker
        if worker:
            try:
                worker.stop()
            except Exception:
                logging.debug("Error stopping worker during detach", exc_info=True)
            self._release_current_frame()
            self._dispose_worker(worker)
            self.worker = None
        
        self.capture_enabled = False
        self.camera_stream_link = None
        if self._latest_frame is not None:
            self._release_current_frame()
        self._last_frame_ts = 0.0
        self._frame_since_attach = False
        self._frame_id = 0
        self._last_rendered_id = -1
        self._restart_events.clear()
        self._restart_limit_logged = False
        self._leaked_worker = False

        self._render_placeholder(self.placeholder_text or "DISCONNECTED")
        
        logging.info("Detached camera %s from widget %s", detached_index, self.widget_id)
        return detached_index
