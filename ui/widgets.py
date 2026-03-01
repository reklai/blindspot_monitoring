"""
UI Widgets for Camera Dashboard.

Contains CameraWidget for camera tiles and FullscreenOverlay for fullscreen view.
"""

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



class FullscreenOverlay(QtWidgets.QWidget):
    """Transparent top-level widget for fullscreen display."""

    def __init__(self, on_click_exit: Callable[[], None]) -> None:
        """Create a full-window view with a centered QLabel."""
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
        """Exit fullscreen on left click/tap."""
        if a0.button() == QtCore.Qt.MouseButton.LeftButton:
            self.on_click_exit()
        super().mousePressEvent(a0)

    def event(self, a0: QtCore.QEvent) -> bool:  # type: ignore[override]
        # Only trigger exit on TouchEnd to prevent double-triggering
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
    """One tile in the grid. Manages UI input and rendering."""

    # How long a press needs to be to enter "swap mode".
    hold_threshold_ms: int = 400

    # Instance type hints
    camera_stream_link: Optional[int]
    worker: Optional[CaptureWorker]
    _fs_overlay: Optional[FullscreenOverlay]

    def __init__(
        self,
        width: int,
        height: int,
        stream_link: Optional[int] = 0,
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
        """Initialize tile UI, worker thread, and timers."""
        super().__init__(parent)
        logging.debug("Creating camera %s", stream_link)

        # Widget configuration: touch enabled, expands in grid, dark theme.
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.setMouseTracking(True)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )

        self.screen_width = max(1, width)
        self.screen_height = max(1, height)
        self.camera_stream_link = stream_link
        self.widget_id = f"cam{stream_link}_{id(self)}"

        # State used for fullscreen toggle + drag/hold swap mode.
        self.is_fullscreen = False
        self.grid_position = None
        self._press_widget_id = None
        self._press_time = 0
        self._grid_parent = None
        self._touch_active = False
        self.swap_active = False
        self._last_fullscreen_toggle_ts = 0.0
        self._fullscreen_debounce_ms = 200  # Minimum ms between fullscreen toggles

        self._fs_overlay = None

        self.capture_enabled = bool(enable_capture)
        self.placeholder_text = placeholder_text
        self.settings_mode = settings_mode
        self.night_mode_enabled = False
        self.brightness = 1.0  # Brightness multiplier: 1.0 = default, <1.0 = darker, >1.0 = brighter

        # Normal state: solid black background; Swap state: yellow border for visual feedback
        self.normal_style = "background: black;"
        self.swap_ready_style = "border: 6px solid #FFFF00; background: black;"
        self.setStyleSheet(self.normal_style)
        self.setObjectName(self.widget_id)

        # QLabel displays video frames or placeholder text; touch events enabled for interaction
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

        # Zero margins/spacing creates seamless grid layout with no borders between tiles
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._layout = layout  # Reference for swap mode margin changes

        # Settings tile provides controls: Restart, Nightmode toggle, and Brightness adjustment
        if self.settings_mode:
            self.video_label.setText("")  # Hide placeholder text
            self.video_label.setFixedSize(0, 0)  # Remove from layout

            # Styled QLabels act as touch-friendly buttons
            btn_style = "QLabel { padding: 8px 12px; margin: 2px; background: #333; color: white; border-radius: 4px; }"

            # Map object names to callbacks for event handling
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

            # Brightness adjustment: 5 levels from dim to max
            brightness_layout = QtWidgets.QHBoxLayout()
            brightness_layout.setSpacing(4)
            self._brightness_buttons = {}
            brightness_values = [15, 60, 80, 100, 150]
            brightness_labels = ["15%", "60%", "80%", "100%", "150%"]

            # Propagate brightness changes to all camera widgets
            self._on_brightness_change = on_brightness_change

            def brightness_callback(v):
                self._set_brightness_value(v)
                if self._on_brightness_change:
                    self._on_brightness_change(v)

            for val, label in zip(brightness_values, brightness_labels):
                btn = QtWidgets.QLabel(label)
                btn = QtWidgets.QLabel(label)
                btn.setStyleSheet(btn_style)
                btn.setAttribute(QtCore.Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
                btn.installEventFilter(self)
                btn.setObjectName(f"brightness_{val}")
                self._brightness_buttons[val] = btn
                # Map button to callback for touch handling
                self._label_buttons[btn.objectName()] = lambda v=val, cb=brightness_callback: cb(v)
                brightness_layout.addWidget(btn)

            self._current_brightness = 100

            # Header label above brightness buttons
            brightness_label = QtWidgets.QLabel("Brightness")
            brightness_label.setStyleSheet("color: white; padding: 4px; font-weight: bold;")
            brightness_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

            # Vertical layout: Restart → Nightmode → Brightness controls
            left_layout = QtWidgets.QVBoxLayout()
            left_layout.addWidget(restart_label, alignment=Qt.AlignmentFlag.AlignCenter)
            left_layout.addSpacing(8)
            left_layout.addWidget(night_mode_label, alignment=Qt.AlignmentFlag.AlignCenter)
            left_layout.addSpacing(8)
            left_layout.addWidget(brightness_label, alignment=Qt.AlignmentFlag.AlignCenter)
            brightness_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            left_layout.addLayout(brightness_layout)

            # Center everything in the settings tile
            main_layout = QtWidgets.QHBoxLayout()
            main_layout.addStretch(1)
            main_layout.addLayout(left_layout, stretch=1)
            main_layout.addStretch(1)

            # Final layout stack
            layout.addStretch(1)
            layout.addLayout(main_layout)
            layout.addStretch(1)
        else:
            layout.addWidget(self.video_label)

        # Render state, staleness tracking, and caches.
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
        self._last_status_log_ts = 0.0
        self._last_status_log_interval_sec = 10.0
        self._pixmap_cache = QtGui.QPixmap()
        self._scaled_pixmap_cache = None
        self._scaled_pixmap_cache_size = None
        self._night_gray = None
        self._night_bgr = None
        # Pre-computed LUT for night mode brightness (1.6x gain, clamped to 255)
        self._night_lut = np.clip(np.arange(256, dtype=np.float32) * 1.6, 0, 255).astype(np.uint8)
        # Brightness LUT - computed dynamically based on brightness setting
        self._brightness_lut = np.arange(256, dtype=np.uint8)  # identity by default

        # Base FPS is the desired target; current FPS is adjusted dynamically.
        self.base_target_fps = target_fps
        self.current_target_fps = target_fps

        # Start capture worker in background thread (if enabled)
        self.worker = None
        if self.capture_enabled and stream_link is not None:
            cap_w, cap_h = (
                request_capture_size if request_capture_size else (None, None)
            )
            self.worker = CaptureWorker(
                stream_link,
                parent=self,
                target_fps=target_fps,
                capture_width=cap_w,
                capture_height=cap_h,
            )
            self.worker.frame_ready.connect(self.on_frame)
            self.worker.status_changed.connect(self.on_status_changed)
            self.worker.start()
        elif not self.settings_mode:
            # No capture: set placeholder immediately
            self._latest_frame = None
            self._render_placeholder(self.placeholder_text or "DISCONNECTED")

        # Timer to render latest frame at a stable UI FPS.
        # Compensate for render overhead to hit actual target FPS.
        if not self.settings_mode:
            self.ui_render_fps = max(1, int(ui_fps))
            self.base_ui_fps = self.ui_render_fps  # Store original for FPS recovery
            interval = max(1, int(1000 / self.ui_render_fps) - config.RENDER_OVERHEAD_MS)
            self.render_timer = QTimer(self)
            self.render_timer.setInterval(interval)
            self.render_timer.timeout.connect(self._render_latest_frame)
            self.render_timer.start()
        else:
            self.ui_render_fps = 0
            self.base_ui_fps = 0
            self.render_timer = None

        # Optional UI FPS diagnostics (only for real cameras)
        if self.capture_enabled and not self.settings_mode and config.UI_FPS_LOGGING:
            self.ui_timer = QTimer(self)
            self.ui_timer.setInterval(1000)
            self.ui_timer.timeout.connect(self._print_fps)
            self.ui_timer.start()
        else:
            self.ui_timer = None

        # Periodic status logging for observability.
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(5000)
        self._status_timer.timeout.connect(self._log_status)
        self._status_timer.start()

        self.installEventFilter(self)
        self.video_label.installEventFilter(self)

        logging.debug("Widget %s ready", self.widget_id)

    def _exit_app(self) -> None:
        """Exit the application gracefully."""
        app = QtWidgets.QApplication.instance()
        if app:
            app.quit()

    def _ensure_fullscreen_overlay(self) -> None:
        """Create fullscreen overlay only when needed."""
        if self._fs_overlay is None:
            self._fs_overlay = FullscreenOverlay(self.exit_fullscreen)

    def _apply_ui_fps(self, ui_fps: int) -> None:
        """Update UI render timer to match camera UI FPS.

        Compensates for render overhead to achieve actual target FPS.
        """
        self.ui_render_fps = max(1, int(ui_fps))
        if self.render_timer:
            interval = max(1, int(1000 / self.ui_render_fps) - config.RENDER_OVERHEAD_MS)
            self.render_timer.setInterval(interval)

    def attach_camera(
        self,
        stream_link: int,
        target_fps: float,
        request_capture_size: tuple[int, int],
        ui_fps: Optional[int] = None,
    ) -> None:
        """Attach a camera to an existing placeholder slot."""
        if self.capture_enabled and self.worker:
            return

        self._restart_events.clear()
        self._restart_limit_logged = False
        self._last_restart_ts = 0.0

        self.capture_enabled = True
        self.camera_stream_link = stream_link
        self.base_target_fps = target_fps
        self.current_target_fps = target_fps

        if ui_fps is not None:
            self._apply_ui_fps(ui_fps)
            self.base_ui_fps = max(1, int(ui_fps))  # Store original for FPS recovery

        cap_w, cap_h = request_capture_size if request_capture_size else (None, None)
        self.worker = CaptureWorker(
            stream_link,
            parent=self,
            target_fps=target_fps,
            capture_width=cap_w,
            capture_height=cap_h,
        )
        self.worker.frame_ready.connect(self.on_frame)
        self.worker.status_changed.connect(self.on_status_changed)
        self.worker.start()

        if self.ui_timer is None and config.UI_FPS_LOGGING:
            self.ui_timer = QTimer(self)
            self.ui_timer.setInterval(1000)
            self.ui_timer.timeout.connect(self._print_fps)
            self.ui_timer.start()

        self._latest_frame = None
        self._render_placeholder("CONNECTING...")
        logging.info("Attached camera %s to widget %s", stream_link, self.widget_id)

    def eventFilter(self, a0: QtCore.QObject, a1: QtCore.QEvent) -> bool:  # type: ignore[override]
        """Route touch/click events to appropriate handlers: settings buttons or camera widgets."""
        # Settings tile: handle touch/click on Restart, Nightmode, and Brightness buttons
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

        # Allow standard button event processing
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
        """Record touch-down timestamp and source widget."""
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
        """Handle touch-up as a click/hold action."""
        try:
            if not self._touch_active:
                return True
            self._touch_active = False
            self._handle_release_as_left_click()
        except Exception:
            logging.exception("touch end")
        return True

    def _handle_release_as_left_click(self) -> bool:
        """
        Unified release handler:
        - short tap: fullscreen toggle
        - long press: swap select
        - swap if another camera is selected
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

            # Cancel swap if tapping the already-selected widget
            if selected == self:
                logging.debug("Clear swap %s", self.widget_id)
                setattr(swap_parent, "selected_camera", None)
                self.swap_active = False
                self.reset_style()
                self._reset_mouse_state()
                return True

            # Complete swap: tap a different widget while one is selected
            if selected and selected != self and not self.is_fullscreen:
                other = selected
                logging.debug("SWAP %s <-> %s", other.widget_id, self.widget_id)
                self.do_swap(other, self, swap_parent)
                other.swap_active = False
                other.reset_style()
                setattr(swap_parent, "selected_camera", None)
                self._reset_mouse_state()
                return True

            # Long press: initiate swap mode (allowed for all tiles including settings)
            if hold_time >= self.hold_threshold_ms and not self.is_fullscreen:
                logging.debug("ENTER swap %s", self.widget_id)
                setattr(swap_parent, "selected_camera", self)
                self.swap_active = True
                self._layout.setContentsMargins(6, 6, 6, 6)  # Expand margin for yellow border
                self.setStyleSheet(self.swap_ready_style)
                self._reset_mouse_state()
                return True

            # Settings tile: don't allow fullscreen on short tap
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
        """Record mouse down position and time."""
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
        """Handle mouse release as click/hold action."""
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return True
        return self._handle_release_as_left_click()

    def _reset_mouse_state(self) -> None:
        """Clear press state to avoid accidental reuse."""
        self._press_time = 0
        self._press_widget_id = None
        self._grid_parent = None

    def do_swap(
        self,
        source: CameraWidget,
        target: CameraWidget,
        layout_parent: Any,
    ) -> None:
        """Swap two widgets inside the grid layout."""
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
        """Toggle between fullscreen and grid view with debounce protection."""
        # Debounce rapid toggles to prevent race conditions
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
        """Enter fullscreen mode for this camera."""
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
        """Exit fullscreen and return to grid view."""
        if not self.is_fullscreen:
            return
        if self._fs_overlay:
            self._fs_overlay.hide()
        self.is_fullscreen = False

    @pyqtSlot(object)
    def on_frame(self, frame_bgr: NDArray[np.uint8]) -> None:
        """Receive latest camera frame from worker."""
        try:
            if frame_bgr is None:
                return
            # Return previous frame to pool before updating _latest_frame.
            # Note: Both on_frame (signal/slot) and _render_latest_frame (timer)
            # run on the main thread via Qt's event loop, so no actual race exists.
            # We return before updating as a defensive pattern for clarity.
            previous_frame = self._latest_frame
            if previous_frame is not None and self.worker is not None:
                try:
                    self.worker.return_frame(previous_frame)
                except Exception:
                    logging.debug("Failed to return frame to pool", exc_info=True)
            # Now safe to update the latest frame
            self._latest_frame = frame_bgr
            self._frame_id += 1
            self._last_frame_ts = time.time()
        except Exception:
            logging.exception("on_frame")

    def _release_current_frame(self, worker: Optional[CaptureWorker] = None) -> None:
        """Return current frame buffer to pool and clear reference."""
        if self._latest_frame is None:
            return
        if worker is None:
            worker = self.worker
        if worker is not None:
            try:
                worker.return_frame(self._latest_frame)
            except Exception:
                logging.debug("Failed to return frame to pool", exc_info=True)
        self._latest_frame = None

    def _dispose_worker(self, worker: CaptureWorker) -> None:
        """Disconnect and schedule a worker for deletion."""
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
            worker.deleteLater()
        except Exception:
            pass

    def _render_placeholder(self, text: str) -> None:
        """Render placeholder text when no frame is available."""
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

    def _render_latest_frame(self) -> None:
        """Convert latest frame to QPixmap and display it."""
        if self.settings_mode:
            return
        try:
            frame_bgr = self._latest_frame
            if frame_bgr is None:
                self._render_placeholder(self.placeholder_text or "DISCONNECTED")
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
                # Reset frame IDs to ensure placeholder renders on next call
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

                    # Lazy allocate night mode buffers (only once per resolution)
                    if self._night_gray is None or self._night_gray.shape != (h, w):
                        self._night_gray = np.empty((h, w), dtype=np.uint8)
                    if self._night_bgr is None or self._night_bgr.shape[:2] != (h, w):
                        # Use contiguous array for efficient Qt buffer access
                        self._night_bgr = np.zeros((h, w, 3), dtype=np.uint8, order='C')

                    if frame_bgr.ndim == 2:
                        # Apply brightness LUT directly to grayscale (in-place)
                        cv2.LUT(frame_bgr, self._night_lut, dst=self._night_gray)
                    else:
                        # Convert to grayscale, then apply brightness LUT (in-place)
                        cv2.cvtColor(
                            frame_bgr, cv2.COLOR_BGR2GRAY, dst=self._night_gray
                        )
                        cv2.LUT(self._night_gray, self._night_lut, dst=self._night_gray)

                    # Optimized: only update red channel, B/G stay zero from allocation
                    # Use direct slice assignment (faster than np.copyto for this pattern)
                    self._night_bgr[:, :, 2] = self._night_gray
                    frame_bgr = self._night_bgr
                except Exception:
                    logging.debug("Night mode processing failed", exc_info=True)

            # Apply brightness adjustment (if not 1.0)
            if self.brightness != 1.0:
                try:
                    if frame_bgr.ndim == 2:
                        temp = np.empty_like(frame_bgr)
                        cv2.LUT(frame_bgr, self._brightness_lut, dst=temp)
                        frame_bgr = temp
                    else:
                        # Apply to each channel
                        for i in range(3):
                            temp = np.empty_like(frame_bgr[:, :, i])
                            cv2.LUT(frame_bgr[:, :, i], self._brightness_lut, dst=temp)
                            frame_bgr[:, :, i] = temp
                except Exception:
                    logging.debug("Brightness processing failed", exc_info=True)

            # Convert numpy frame to Qt image, handling grayscale or BGR.
            # Ensure contiguous memory layout for direct buffer access (avoids copy).
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

            # Fullscreen scales to screen size; grid uses label size.
            if self.is_fullscreen and self._fs_overlay:
                if target_size.width() > 0 and target_size.height() > 0:
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
                    self._fs_overlay.label.setPixmap(self._scaled_pixmap_cache)
                else:
                    self._fs_overlay.label.setPixmap(self._pixmap_cache)
                self._fs_overlay.label.setText("")
            else:
                if (
                    target_size.width() > 0
                    and target_size.height() > 0
                    and self._pixmap_cache.size() != target_size
                ):
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
                    self.video_label.setPixmap(self._scaled_pixmap_cache)
                else:
                    self.video_label.setPixmap(self._pixmap_cache)
                self.video_label.setText("")

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
        """Update UI when camera goes online or offline."""
        if online:
            # Preserve yellow border if swap mode is active
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
        """Restore default border styling and margins."""
        self.video_label.setStyleSheet("")
        if self.swap_active:
            self._layout.setContentsMargins(6, 6, 6, 6)
            self.setStyleSheet(self.swap_ready_style)
        else:
            self._layout.setContentsMargins(2, 2, 2, 2)
            self.setStyleSheet(self.normal_style)

    def _print_fps(self) -> None:
        """Log rendering FPS for this widget."""
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
        """Apply dynamic FPS change from stress monitor."""
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
        """Apply dynamic UI FPS change from stress monitor."""
        if self.settings_mode:
            return
        try:
            ui_fps = int(ui_fps)
            if ui_fps < config.MIN_DYNAMIC_UI_FPS:
                ui_fps = config.MIN_DYNAMIC_UI_FPS
            self._apply_ui_fps(ui_fps)
        except Exception:
            logging.exception("set_dynamic_ui_fps")

    def _restart_capture_if_stale(self) -> None:
        """Restart the capture worker after a stale frame timeout."""
        if not self.capture_enabled or not self.worker:
            return
        now = time.time()
        if (now - self._last_restart_ts) < self._restart_cooldown_sec:
            return
        recent = [
            t for t in self._restart_events if (now - t) <= self._restart_window_sec
        ]
        if len(recent) >= self._max_restarts_per_window:
            # Don't give up forever - schedule a retry after extended cooldown
            extended_cooldown = self._restart_window_sec * 2  # 60 seconds
            if (now - self._last_restart_ts) < extended_cooldown:
                if not getattr(self, '_restart_limit_logged', False):
                    logging.warning(
                        "Restart limit reached for %s, will retry in %.0fs",
                        self.camera_stream_link,
                        extended_cooldown
                    )
                    self._restart_limit_logged = True
                return
            # Extended cooldown passed, clear events and allow restart
            logging.info(
                "Extended cooldown passed for %s, attempting recovery",
                self.camera_stream_link
            )
            self._restart_events.clear()
            self._restart_limit_logged = False
        
        self._restart_events.append(now)
        self._last_restart_ts = now
        
        # Store old worker reference to verify cleanup
        old_worker = self.worker
        cap_w = getattr(old_worker, "capture_width", None)
        cap_h = getattr(old_worker, "capture_height", None)
        target_fps = self.current_target_fps or self.base_target_fps
        
        logging.info(
            "Restarting capture for %s after stale frames", self.camera_stream_link
        )
        
        # Stop old worker and verify it stopped
        try:
            old_worker.stop()
        except Exception:
            logging.exception("Error stopping old worker for %s", self.camera_stream_link)
        
        # Verify old worker is actually stopped before creating new one
        if old_worker.isRunning():
            logging.error(
                "Old worker for %s still running after stop() - potential resource leak",
                self.camera_stream_link
            )
            # Don't create a new worker if old one is still running
            # This prevents resource conflicts
            return

        self._dispose_worker(old_worker)
        
        # camera_stream_link is guaranteed to be set if capture_enabled is True
        if self.camera_stream_link is None:
            return
        
        self.worker = CaptureWorker(
            self.camera_stream_link,
            parent=self,
            target_fps=target_fps,
            capture_width=cap_w,
            capture_height=cap_h,
        )
        self.worker.frame_ready.connect(self.on_frame)
        self.worker.status_changed.connect(self.on_status_changed)
        self.worker.start()
        self._render_placeholder("CONNECTING...")

    def _log_status(self) -> None:
        """Periodic status log for observability."""
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
        """Enable or disable night mode rendering."""
        self.night_mode_enabled = bool(enabled)

    def set_night_mode_button_label(self, enabled: bool) -> None:
        """Update settings tile button label for night mode."""
        if self.settings_mode and hasattr(self, "night_mode_button"):
            label = "Nightmode: On" if enabled else "Nightmode: Off"
            self.night_mode_button.setText(label)

    def set_brightness(self, value: float) -> None:
        """Apply brightness multiplier to camera output (1.0 = default, <1.0 = darker, >1.0 = brighter)."""
        self.brightness = max(0.5, min(3.0, value))
        # Pre-compute LUT for efficient per-pixel brightness adjustment
        input_vals = np.arange(256, dtype=np.float32)
        if self.brightness < 1.0:
            # Darker: squash the input range to a smaller output range
            max_out = 255 * self.brightness
            self._brightness_lut = (input_vals * (max_out / 255.0)).astype(np.uint8)
        else:
            # Brighter: amplify then clamp to prevent overflow
            self._brightness_lut = np.clip(input_vals * self.brightness, 0, 255).astype(np.uint8)
        # Highlight selected button
        self._update_brightness_buttons()

    def _set_brightness_value(self, value: int) -> None:
        """Convert percentage value (15-150) to brightness factor and apply."""
        self._current_brightness = value
        brightness_factor = value / 100.0
        self.set_brightness(brightness_factor)
        self._update_brightness_buttons()

    def _update_brightness_buttons(self) -> None:
        """Highlight the currently selected brightness level."""
        if not hasattr(self, '_brightness_buttons'):
            return
        btn_style = "QLabel { padding: 8px 12px; margin: 2px; background: #333; color: white; border-radius: 4px; }"
        selected_style = "QLabel { padding: 8px 12px; margin: 2px; background: #666; color: white; border-radius: 4px; font-weight: bold; }"
        for val, btn in self._brightness_buttons.items():
            btn.setStyleSheet(selected_style if val == self._current_brightness else btn_style)

    def cleanup(self) -> None:
        """Stop the capture worker thread cleanly."""
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
                self._release_current_frame(worker)
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

    def detach_camera(self) -> Optional[int]:
        """Detach camera from this widget and return to placeholder state.
        
        Returns the camera index that was detached, or None if not applicable.
        """
        if not self.capture_enabled or self.settings_mode:
            return None
        
        detached_index = self.camera_stream_link
        
        # Stop capture worker
        worker = self.worker
        if worker:
            try:
                worker.stop()
            except Exception:
                logging.debug("Error stopping worker during detach", exc_info=True)
            self._release_current_frame(worker)
            self._dispose_worker(worker)
            self.worker = None
        
        # Reset to placeholder state
        self.capture_enabled = False
        self.camera_stream_link = None
        if self._latest_frame is not None:
            self._release_current_frame()
        self._last_frame_ts = 0.0
        self._frame_id = 0
        self._last_rendered_id = -1
        self._restart_events.clear()
        self._restart_limit_logged = False
        
        # Update display
        self._render_placeholder(self.placeholder_text or "DISCONNECTED")
        
        logging.info("Detached camera %s from widget %s", detached_index, self.widget_id)
        return detached_index
