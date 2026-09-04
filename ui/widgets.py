"""
Tiles for the camera grid.

``CameraWidget`` is one tile. It owns a ``CaptureWorker`` when a camera is
attached, renders that worker's latest frame on a timer, watches for the
frame stream going stale, and handles the touch/mouse gestures shared by
every tile (tap for fullscreen, long-press to select for a swap). The same
class, with ``settings_mode=True``, hosts the settings controls instead of
a video label; those controls live in ``SettingsControls`` so the tile
itself only knows that it has no video.

Threading: all methods here run on the UI thread. Frames arrive through a
queued signal from the worker thread; the render timer picks up whichever
frame is newest, so a slow render never queues stale pictures.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt, QTimer, pyqtSlot

from core import config
from core.camera import CaptureWorker
from core.recovery import RestartBudget, RestartVerdict
from ui.render import Frame, FrameStyler

PLACEHOLDER_DISCONNECTED = "DISCONNECTED"
PLACEHOLDER_CONNECTING = "CONNECTING..."
PLACEHOLDER_STYLE = "color: #bbbbbb; font-size: 24px;"

# Workers that ignored stop() are parked here for the life of the process.
# Deleting a running QThread aborts the process, so they must outlive their
# tile; keeping a reference is the only safe thing left to do with them.
_parked_workers: list[CaptureWorker] = []

# Identity styler used when a tile's own styler raises on a frame.
_RAW_STYLER = FrameStyler()


class FullscreenOverlay(QtWidgets.QWidget):
    """Frameless top-level window that shows one tile's video full screen.

    It is a separate top-level window rather than a widget raised inside the
    grid so it can cover the whole screen without disturbing the grid
    layout underneath; the tile keeps rendering into whichever label is
    visible (see CameraWidget._render_target). Created lazily on the first
    fullscreen request and reused after that.
    """

    def __init__(self, on_click_exit: Callable[[], None]) -> None:
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
        if a0.button() == QtCore.Qt.MouseButton.LeftButton:
            self.on_click_exit()
        super().mousePressEvent(a0)

    def event(self, a0: QtCore.QEvent) -> bool:  # type: ignore[override]
        # Exit on TouchEnd only, so a tap does not also arrive as a synthetic
        # mouse press and exit twice.
        if a0.type() == QtCore.QEvent.Type.TouchBegin:
            self._touch_active = True
            return True
        if a0.type() == QtCore.QEvent.Type.TouchEnd:
            if self._touch_active:
                self._touch_active = False
                self.on_click_exit()
            return True
        return super().event(a0)


class SettingsControls(QtCore.QObject):
    """Restart / night mode / brightness buttons for the settings tile.

    Buttons are styled ``QLabel``s so they get the same touch treatment as
    the tiles. This object builds them into the tile's layout and filters
    their input events; the tile keeps receiving presses on the empty area
    around them, which is how the settings tile joins swap mode.
    """

    BUTTON_STYLE = (
        "QLabel { padding: 8px 12px; margin: 2px; background: #333; "
        "color: white; border-radius: 4px; }"
    )
    SELECTED_STYLE = (
        "QLabel { padding: 8px 12px; margin: 2px; background: #666; "
        "color: white; border-radius: 4px; font-weight: bold; }"
    )
    BRIGHTNESS_PRESETS = (15, 60, 80, 100, 150)

    def __init__(
        self,
        tile: QtWidgets.QWidget,
        layout: QtWidgets.QVBoxLayout,
        on_restart: Optional[Callable[[], None]],
        on_night_mode_toggle: Optional[Callable[[], None]],
        on_brightness_change: Optional[Callable[[int], None]],
    ) -> None:
        super().__init__(tile)
        self._actions: dict[QtCore.QObject, Callable[[], None]] = {}
        self._touch_active = False
        self._on_brightness_change = on_brightness_change

        restart = self._button("Restart", on_restart)
        self.night_mode_button = self._button("Nightmode: Off", on_night_mode_toggle)

        self._brightness_buttons: dict[int, QtWidgets.QLabel] = {}
        brightness_row = QtWidgets.QHBoxLayout()
        brightness_row.setSpacing(4)
        brightness_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for percent in self.BRIGHTNESS_PRESETS:
            button = self._button(f"{percent}%", lambda p=percent: self._pick_brightness(p))
            self._brightness_buttons[percent] = button
            brightness_row.addWidget(button)

        heading = QtWidgets.QLabel("Brightness")
        heading.setStyleSheet("color: white; padding: 4px; font-weight: bold;")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)

        column = QtWidgets.QVBoxLayout()
        column.addWidget(restart, alignment=Qt.AlignmentFlag.AlignCenter)
        column.addSpacing(8)
        column.addWidget(self.night_mode_button, alignment=Qt.AlignmentFlag.AlignCenter)
        column.addSpacing(8)
        column.addWidget(heading, alignment=Qt.AlignmentFlag.AlignCenter)
        column.addLayout(brightness_row)

        centered = QtWidgets.QHBoxLayout()
        centered.addStretch(1)
        centered.addLayout(column, stretch=1)
        centered.addStretch(1)

        layout.addStretch(1)
        layout.addLayout(centered)
        layout.addStretch(1)

    def _button(self, text: str, action: Optional[Callable[[], None]]) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setStyleSheet(self.BUTTON_STYLE)
        label.setAttribute(QtCore.Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        label.installEventFilter(self)
        # A button with no callback still swallows its presses; otherwise the
        # press would fall through to the tile and count toward a long-press.
        self._actions[label] = action if action is not None else (lambda: None)
        return label

    def _pick_brightness(self, percent: int) -> None:
        for value, button in self._brightness_buttons.items():
            button.setStyleSheet(self.SELECTED_STYLE if value == percent else self.BUTTON_STYLE)
        if self._on_brightness_change is not None:
            self._on_brightness_change(percent)

    def set_night_mode_label(self, enabled: bool) -> None:
        self.night_mode_button.setText("Nightmode: On" if enabled else "Nightmode: Off")

    def eventFilter(self, a0: QtCore.QObject, a1: QtCore.QEvent) -> bool:  # type: ignore[override]
        action = self._actions.get(a0)
        if action is None:
            return super().eventFilter(a0, a1)
        kind = a1.type()
        if kind == QtCore.QEvent.Type.TouchBegin:
            self._touch_active = True
            return True
        if kind == QtCore.QEvent.Type.TouchEnd:
            if self._touch_active:
                self._touch_active = False
                action()
            return True
        if kind == QtCore.QEvent.Type.MouseButtonPress:
            return True
        if kind == QtCore.QEvent.Type.MouseButtonRelease:
            action()
            return True
        return super().eventFilter(a0, a1)


class CameraWidget(QtWidgets.QWidget):
    """One tile in the grid. Manages UI input and rendering."""

    # How long a press needs to be to enter "swap mode". Shorter than a
    # typical OS long-press so a gloved driver does not have to hold on.
    hold_threshold_ms: int = 400
    # Minimum ms between fullscreen toggles. A touch tap arrives as TouchEnd
    # and again as a synthesised mouse release; without this it toggles twice.
    fullscreen_debounce_ms: int = 200
    # Log interval for the per-tile status line.
    status_log_interval_sec: float = 10.0

    normal_style = "background: black;"
    swap_ready_style = "border: 6px solid #FFFF00; background: black;"

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
        super().__init__(parent)
        logging.debug("Creating camera %s", stream_link)

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
        self.setObjectName(self.widget_id)
        self.setStyleSheet(self.normal_style)

        self.capture_enabled = bool(enable_capture)
        self.placeholder_text = placeholder_text
        self.settings_mode = settings_mode

        # Gesture state: fullscreen toggle and press-and-hold swap mode.
        # _press_widget_id records which tile saw the press so a release
        # delivered to a different tile (finger slid) is ignored.
        self.is_fullscreen = False
        self.grid_position: Optional[tuple[int, int]] = None
        self.swap_active = False
        self._fs_overlay = None
        self._press_widget_id: Optional[str] = None
        self._press_time = 0.0
        self._grid_parent: Optional[QtCore.QObject] = None
        self._touch_active = False
        self._last_fullscreen_toggle_ts = 0.0

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
        self.video_label.setAttribute(QtCore.Qt.WidgetAttribute.WA_AcceptTouchEvents, True)

        # Zero margins so tiles butt up against each other with no seams.
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._layout = layout

        self._settings: Optional[SettingsControls] = None
        if self.settings_mode:
            self.video_label.setText("")
            self.video_label.setFixedSize(0, 0)
            self._settings = SettingsControls(
                self, layout, on_restart, on_night_mode_toggle, on_brightness_change
            )
        else:
            layout.addWidget(self.video_label)

        # Frame state. `_frame_id` increments per received frame so the
        # render timer can skip work when nothing changed; `_last_rendered_size`
        # makes a resize (grid <-> fullscreen) repaint the same frame.
        # `_last_frame_ts` drives stale detection and is also refreshed when
        # the worker reports online, which gives a freshly opened device a
        # full timeout to produce its first frame.
        self._latest_frame: Optional[Frame] = None
        self._frame_id = 0
        self._last_rendered_id = -1
        self._last_rendered_size: Optional[QtCore.QSize] = None
        self._last_frame_ts = 0.0
        self._last_placeholder_text: Optional[str] = None
        self._last_placeholder_fullscreen: Optional[bool] = None
        self._pixmap_cache = QtGui.QPixmap()
        self._scaled_pixmap_cache: Optional[QtGui.QPixmap] = None
        self._styler = FrameStyler()
        self._styler_failed = False
        self.night_mode_enabled = False
        self.brightness = 1.0

        # Stale-frame recovery.
        self._stale_frame_timeout_sec = config.STALE_FRAME_TIMEOUT_SEC
        self.restart_budget = RestartBudget(
            cooldown_sec=config.RESTART_COOLDOWN_SEC,
            window_sec=config.RESTART_WINDOW_SEC,
            max_per_window=config.MAX_RESTARTS_PER_WINDOW,
        )
        self._last_status_log_ts = 0.0

        # Diagnostics counters for optional UI FPS logging.
        self.frame_count = 0
        self.prev_time = time.time()

        # Base FPS is what the profile asked for; current is after dynamic adjustment.
        self.base_target_fps = target_fps
        self.current_target_fps = target_fps

        self.worker = None
        if self.capture_enabled and stream_link is not None:
            self._start_worker(stream_link, target_fps, request_capture_size)
        elif not self.settings_mode:
            self._render_placeholder(self.placeholder_text or PLACEHOLDER_DISCONNECTED)

        # Render timer: paints the newest frame at a steady UI rate.
        if self.settings_mode:
            self.ui_render_fps = 0
            self.base_ui_fps = 0
            self.render_timer = None
        else:
            self.ui_render_fps = max(1, int(ui_fps))
            self.base_ui_fps = self.ui_render_fps
            self.render_timer = QTimer(self)
            self.render_timer.setInterval(self._render_interval_ms(self.ui_render_fps))
            self.render_timer.timeout.connect(self._render_latest_frame)
            self.render_timer.start()

        self.ui_timer: Optional[QTimer] = None
        if self.capture_enabled and not self.settings_mode and config.UI_FPS_LOGGING:
            self._start_fps_logging()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(5000)
        self._status_timer.timeout.connect(self._log_status)
        self._status_timer.start()

        # The label covers the whole tile, so input lands on it, not on the
        # tile; filter both so gestures work whichever one Qt targets.
        self.installEventFilter(self)
        self.video_label.installEventFilter(self)

        logging.debug("Widget %s ready", self.widget_id)

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    def _start_worker(
        self,
        stream_link: int,
        target_fps: Optional[float],
        request_capture_size: Optional[tuple[Optional[int], Optional[int]]],
    ) -> None:
        cap_w, cap_h = request_capture_size if request_capture_size else (None, None)
        worker = CaptureWorker(
            stream_link,
            parent=self,
            target_fps=target_fps,
            capture_width=cap_w,
            capture_height=cap_h,
        )
        worker.frame_ready.connect(self.on_frame)
        worker.status_changed.connect(self.on_status_changed)
        worker.start()
        self.worker = worker

    def _retire_worker(self, worker: CaptureWorker) -> bool:
        """Stop a worker and release it. Returns False if it would not stop.

        UI thread. Either way the worker is disconnected from this tile
        first: a worker that ignores stop() may still be blocked in the
        driver, and if that call ever returns its frames must not paint on
        a tile that has since been given another camera. Such a worker is
        then unparented and parked rather than deleted, because deleting a
        running QThread aborts the process.
        """
        try:
            worker.stop()
        except Exception:
            logging.exception("Error stopping worker for %s", self.camera_stream_link)
        for signal, slot in (
            (worker.frame_ready, self.on_frame),
            (worker.status_changed, self.on_status_changed),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        try:
            worker.setParent(None)
        except RuntimeError:
            pass
        if worker.isRunning():
            if worker not in _parked_workers:
                _parked_workers.append(worker)
            return False
        try:
            worker.deleteLater()
        except RuntimeError:
            pass
        return True

    def attach_camera(
        self,
        stream_link: int,
        target_fps: float,
        request_capture_size: tuple[int, int],
        ui_fps: Optional[int] = None,
    ) -> None:
        """Turn a placeholder tile into a live camera tile."""
        if self.capture_enabled and self.worker:
            return

        self.restart_budget.reset()
        self.capture_enabled = True
        self.camera_stream_link = stream_link
        self.base_target_fps = target_fps
        self.current_target_fps = target_fps

        if ui_fps is not None:
            self._apply_ui_fps(ui_fps)
            self.base_ui_fps = max(1, int(ui_fps))

        self._start_worker(stream_link, target_fps, request_capture_size)
        if self.ui_timer is None and config.UI_FPS_LOGGING:
            self._start_fps_logging()

        self._latest_frame = None
        self._render_placeholder(PLACEHOLDER_CONNECTING)
        logging.info("Attached camera %s to widget %s", stream_link, self.widget_id)

    def detach_camera(self) -> Optional[int]:
        """Turn a live camera tile back into a placeholder.

        Returns the detached camera index, or None if there was nothing to
        detach.
        """
        if not self.capture_enabled or self.settings_mode:
            return None

        detached_index = self.camera_stream_link
        if self.worker is not None:
            self._retire_worker(self.worker)
            self.worker = None

        self.capture_enabled = False
        self.camera_stream_link = None
        self._latest_frame = None
        self._last_frame_ts = 0.0
        self._frame_id = 0
        self._last_rendered_id = -1
        self.restart_budget.reset()
        self._render_placeholder(self.placeholder_text or PLACEHOLDER_DISCONNECTED)

        logging.info("Detached camera %s from widget %s", detached_index, self.widget_id)
        return detached_index

    def should_detach(self, now: float) -> bool:
        """True when restarts are exhausted, the extended cooldown has passed,
        and the camera is still not delivering frames."""
        return (
            self.capture_enabled
            and self._latest_frame is None
            and self.restart_budget.is_beyond_extended_cooldown(now)
        )

    def cleanup(self) -> None:
        """Stop timers and the worker; safe to call more than once."""
        try:
            for timer in (self.render_timer, self.ui_timer, self._status_timer):
                if timer is not None and timer.isActive():
                    timer.stop()

            worker = getattr(self, "worker", None)
            if worker is not None:
                self._retire_worker(worker)
                self._latest_frame = None
                self.worker = None

            if self._fs_overlay is not None:
                try:
                    self._fs_overlay.hide()
                    self._fs_overlay.setParent(None)
                    self._fs_overlay.deleteLater()
                except RuntimeError:
                    pass
                self._fs_overlay = None
                self.is_fullscreen = False
        except Exception:
            logging.debug("cleanup failed for %s", self.widget_id, exc_info=True)

    def _restart_capture_if_stale(self) -> None:
        """Replace the worker after a stale-frame timeout, within budget.

        Known limitation (pre-dates this code): stale detection only runs
        while a frame is held, so a camera that opens and never delivers a
        frame is restarted at most once per frame it did deliver. The
        worker's own reconnect loop covers the unplugged case; this path is
        for a worker that wedges after streaming. See docs/performance-avenues.md.

        Clocks: budget and staleness use ``time.time()``. A wall-clock step
        (NTP sync shortly after boot) can therefore trigger one spurious
        stale restart on every camera; harmless, but expect it in the logs.
        """
        if not self.capture_enabled or not self.worker:
            return
        now = time.time()
        was_exhausted = self.restart_budget.exhausted
        verdict = self.restart_budget.request(now)
        if verdict is RestartVerdict.COOLING_DOWN:
            return
        if verdict is RestartVerdict.EXHAUSTED:
            if not was_exhausted:
                logging.warning(
                    "Restart limit reached for %s, will retry in %.0fs",
                    self.camera_stream_link,
                    self.restart_budget.extended_cooldown_sec,
                )
            return
        if verdict is RestartVerdict.RECOVERED:
            logging.info(
                "Extended cooldown passed for %s, attempting recovery",
                self.camera_stream_link,
            )

        old_worker = self.worker
        logging.info("Restarting capture for %s after stale frames", self.camera_stream_link)
        if not self._retire_worker(old_worker):
            logging.error(
                "Old worker for %s still running after stop() - potential resource leak",
                self.camera_stream_link,
            )
            return

        if self.camera_stream_link is None:
            return
        self._start_worker(
            self.camera_stream_link,
            self.current_target_fps or self.base_target_fps,
            (old_worker.capture_width, old_worker.capture_height),
        )
        self._render_placeholder(PLACEHOLDER_CONNECTING)

    # ------------------------------------------------------------------
    # Frame intake and rendering
    # ------------------------------------------------------------------

    @pyqtSlot(object)
    def on_frame(self, frame_bgr: Frame) -> None:
        """Keep the newest frame; the render timer will paint it.

        UI thread, via queued connection. From here the tile owns the array:
        the worker dropped its reference on emit, and the styler never
        writes to it, so it is safe to hold until the next frame replaces it.
        """
        if frame_bgr is None:
            return
        self._latest_frame = frame_bgr
        self._frame_id += 1
        self._last_frame_ts = time.time()

    @pyqtSlot(bool)
    def on_status_changed(self, online: bool) -> None:
        # Queued from the worker thread. "online" means the device opened;
        # frames may still take a moment, hence the timestamp refresh.
        if online:
            self.setStyleSheet(self.swap_ready_style if self.swap_active else self.normal_style)
            self.video_label.setText("")
            self._last_frame_ts = time.time()
        else:
            self._latest_frame = None
            self._last_rendered_id = -1
            self._render_placeholder(PLACEHOLDER_DISCONNECTED)

    @staticmethod
    def _render_interval_ms(ui_fps: int) -> int:
        # Shave the average render cost off the period so the achieved rate
        # lands on the target instead of just under it.
        return max(1, int(1000 / max(1, ui_fps)) - config.RENDER_OVERHEAD_MS)

    def _apply_ui_fps(self, ui_fps: int) -> None:
        self.ui_render_fps = max(1, int(ui_fps))
        if self.render_timer:
            self.render_timer.setInterval(self._render_interval_ms(self.ui_render_fps))

    def _render_target(self) -> tuple[QtWidgets.QLabel, QtCore.QSize]:
        if self.is_fullscreen and self._fs_overlay is not None:
            return self._fs_overlay.label, self._fs_overlay.size()
        return self.video_label, self.video_label.size()

    def _render_placeholder(self, text: str) -> None:
        if self.settings_mode:
            return
        # Called every render tick while there is no frame, so skip the label
        # update unless the text or the target label changed. Swap mode
        # always re-applies its border style, which setText would otherwise
        # leave stale.
        if (
            text == self._last_placeholder_text
            and not self.swap_active
            and self.is_fullscreen == self._last_placeholder_fullscreen
        ):
            return
        label, _ = self._render_target()
        label.setPixmap(QtGui.QPixmap())
        label.setText(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(PLACEHOLDER_STYLE)
        self._last_placeholder_text = text
        self._last_placeholder_fullscreen = self.is_fullscreen
        if self.swap_active:
            self.setStyleSheet(self.swap_ready_style)

    def _render_latest_frame(self) -> None:
        if self.settings_mode:
            return
        try:
            frame = self._latest_frame
            if frame is None:
                self._render_placeholder(self.placeholder_text or PLACEHOLDER_DISCONNECTED)
                return

            now = time.time()
            if self._last_frame_ts and (now - self._last_frame_ts) > self._stale_frame_timeout_sec:
                self._handle_stale(now - self._last_frame_ts)
                return

            label, target_size = self._render_target()
            if self._frame_id == self._last_rendered_id and self._last_rendered_size == target_size:
                return

            self._pixmap_cache.convertFromImage(self._style_or_raw(frame))
            self._present(label, target_size)

            self._last_rendered_id = self._frame_id
            self._last_rendered_size = target_size
            self._last_placeholder_text = None
            self._last_placeholder_fullscreen = None
            if config.UI_FPS_LOGGING:
                self.frame_count += 1
        except Exception:
            logging.exception("render frame")

    def _style_or_raw(self, frame: Frame) -> QtGui.QImage:
        """Style the frame; on failure paint it unstyled rather than blank.

        A frame shape the styler cannot handle (a 4-channel buffer from an
        unusual backend, say) must not turn into a dead tile with an ERROR
        line 20 times a second. Logged once per tile.
        """
        try:
            return self._styler.to_qimage(frame)
        except Exception:
            if not self._styler_failed:
                self._styler_failed = True
                logging.warning(
                    "Camera %s: styling failed, showing raw frames",
                    self.camera_stream_link,
                    exc_info=True,
                )
            return _RAW_STYLER.to_qimage(frame)

    def _handle_stale(self, stale_for: float) -> None:
        # Dropping the frame matters: with no frame the render loop goes to
        # the placeholder path and stops calling this, so one stall produces
        # one restart request, not one per tick. The next frame from the new
        # worker re-arms stale detection.
        logging.warning(
            "Camera %s: Stale frame detected (no frames for %.1fs)",
            self.camera_stream_link,
            stale_for,
        )
        self._latest_frame = None
        self._last_rendered_id = -1
        self._render_placeholder(PLACEHOLDER_DISCONNECTED)
        self._restart_capture_if_stale()

    def _present(self, label: QtWidgets.QLabel, target_size: QtCore.QSize) -> None:
        """Put the cached pixmap on ``label``, pre-scaled to ``target_size``.

        Scaling here (rather than letting the label do it on paint) keeps
        the cost predictable and lets the scaled buffer be reused.
        """
        pixmap = self._pixmap_cache
        if (
            target_size.width() > 0
            and target_size.height() > 0
            and pixmap.size() != target_size
        ):
            scaled = self._scaled_pixmap_cache
            if scaled is None or scaled.size() != target_size:
                scaled = QtGui.QPixmap(target_size)
                self._scaled_pixmap_cache = scaled
            scaled.fill(Qt.GlobalColor.black)
            painter = QtGui.QPainter(scaled)
            painter.drawPixmap(QtCore.QRect(QtCore.QPoint(0, 0), target_size), pixmap)
            painter.end()
            pixmap = scaled
        label.setPixmap(pixmap)
        label.setText("")

    # ------------------------------------------------------------------
    # Appearance and rate controls (called from the dashboard)
    # ------------------------------------------------------------------

    def set_night_mode(self, enabled: bool) -> None:
        self.night_mode_enabled = bool(enabled)
        self._styler.set_night_mode(self.night_mode_enabled)
        self._last_rendered_id = -1  # repaint the current frame with the new look

    def set_night_mode_button_label(self, enabled: bool) -> None:
        if self._settings is not None:
            self._settings.set_night_mode_label(enabled)

    def set_brightness(self, value: float) -> None:
        """Set the brightness multiplier (1.0 = as captured); clamped by the styler."""
        self._styler.set_brightness(value)
        self.brightness = self._styler.brightness
        self._last_rendered_id = -1

    def set_dynamic_fps(self, fps: Optional[float]) -> None:
        """Apply a capture-rate change from the stress monitor."""
        if fps is None or not self.capture_enabled:
            return
        try:
            fps = max(float(fps), float(config.MIN_DYNAMIC_FPS))
        except (TypeError, ValueError):
            return
        self.current_target_fps = fps
        if self.worker:
            self.worker.set_target_fps(fps)

    def set_dynamic_ui_fps(self, ui_fps: int) -> None:
        """Apply a render-rate change from the stress monitor."""
        if self.settings_mode:
            return
        try:
            ui_fps = max(int(ui_fps), int(config.MIN_DYNAMIC_UI_FPS))
        except (TypeError, ValueError):
            return
        self._apply_ui_fps(ui_fps)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def _start_fps_logging(self) -> None:
        self.ui_timer = QTimer(self)
        self.ui_timer.setInterval(1000)
        self.ui_timer.timeout.connect(self._print_fps)
        self.ui_timer.start()

    def _print_fps(self) -> None:
        if not config.UI_FPS_LOGGING:
            return
        now = time.time()
        elapsed = now - self.prev_time
        if elapsed >= 1.0:
            logging.info("%s FPS: %.1f", self.widget_id, self.frame_count / elapsed)
            self.frame_count = 0
            self.prev_time = now

    def _log_status(self) -> None:
        if self.settings_mode or self.camera_stream_link is None:
            return
        now = time.time()
        if (now - self._last_status_log_ts) < self.status_log_interval_sec:
            return
        self._last_status_log_ts = now
        logging.info(
            "Camera %s status online=%s fps=%.1f ui_fps=%d fourcc=%s",
            self.camera_stream_link,
            "yes" if self._latest_frame is not None else "no",
            float(self.current_target_fps or 0),
            int(self.ui_render_fps or 0),
            self.worker.get_fourcc() if self.worker is not None else "unknown",
        )

    # ------------------------------------------------------------------
    # Gestures: tap = fullscreen, hold = swap select
    # ------------------------------------------------------------------

    def eventFilter(self, a0: QtCore.QObject, a1: QtCore.QEvent) -> bool:  # type: ignore[override]
        if a0 not in (self, self.video_label) or a1 is None:
            return super().eventFilter(a0, a1)
        kind = a1.type()
        if kind == QtCore.QEvent.Type.TouchBegin:
            return self._on_touch_begin(a1)
        if kind == QtCore.QEvent.Type.TouchEnd:
            return self._on_touch_end()
        if kind == QtCore.QEvent.Type.MouseButtonPress:
            return self._on_mouse_press(a1)
        if kind == QtCore.QEvent.Type.MouseButtonRelease:
            return self._on_mouse_release(a1)
        return super().eventFilter(a0, a1)

    def _begin_press(self) -> None:
        self._press_time = time.time() * 1000.0
        self._press_widget_id = self.widget_id
        self._grid_parent = self.parent()

    def _reset_mouse_state(self) -> None:
        self._press_time = 0.0
        self._press_widget_id = None
        self._grid_parent = None

    def _on_touch_begin(self, event: Any) -> bool:
        try:
            points = event.points()
            if len(points) == 1:
                self._touch_active = True
                self._begin_press()
                logging.debug("Touch begin %s", self.widget_id)
        except Exception:
            logging.exception("touch begin")
        return True

    def _on_touch_end(self) -> bool:
        try:
            if self._touch_active:
                self._touch_active = False
                self._handle_release_as_left_click()
        except Exception:
            logging.exception("touch end")
        return True

    def _on_mouse_press(self, event: Any) -> bool:
        try:
            if event.button() == QtCore.Qt.MouseButton.LeftButton:
                self._begin_press()
                logging.debug("Press %s", self.widget_id)
            elif event.button() == QtCore.Qt.MouseButton.RightButton:
                self.toggle_fullscreen()
        except Exception:
            logging.exception("mouse press")
        return True

    def _on_mouse_release(self, event: Any) -> bool:
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return True
        return self._handle_release_as_left_click()

    def _handle_release_as_left_click(self) -> bool:
        """Resolve a press/release pair into a tap, a hold, or a swap.

        With a tile already selected for swapping, a tap on any other tile
        completes the swap and a tap on the selected tile cancels it. Otherwise
        a hold selects this tile and a tap toggles fullscreen. The settings
        tile can be swapped but never goes fullscreen.
        """
        try:
            if self._press_widget_id != self.widget_id:
                return True
            hold_time = (time.time() * 1000.0) - self._press_time
            logging.debug("Release %s hold=%dms", self.widget_id, int(hold_time))

            grid = self._grid_parent
            if grid is None or not hasattr(grid, "selected_camera"):
                if not self.settings_mode:
                    self.toggle_fullscreen()
                return True

            selected = getattr(grid, "selected_camera", None)
            if selected is self:
                logging.debug("Clear swap %s", self.widget_id)
                setattr(grid, "selected_camera", None)
                self.swap_active = False
                self.reset_style()
                return True

            if selected is not None and not self.is_fullscreen:
                logging.debug("SWAP %s <-> %s", selected.widget_id, self.widget_id)
                self.do_swap(selected, self, grid)
                selected.swap_active = False
                selected.reset_style()
                setattr(grid, "selected_camera", None)
                return True

            if hold_time >= self.hold_threshold_ms and not self.is_fullscreen:
                logging.debug("ENTER swap %s", self.widget_id)
                setattr(grid, "selected_camera", self)
                self.swap_active = True
                self._layout.setContentsMargins(6, 6, 6, 6)  # room for the border
                self.setStyleSheet(self.swap_ready_style)
                return True

            if not self.settings_mode:
                logging.debug("Short tap fullscreen %s", self.widget_id)
                self.toggle_fullscreen()
        except Exception:
            logging.exception("touch release")
        finally:
            self._reset_mouse_state()
        return True

    def do_swap(
        self,
        source: CameraWidget,
        target: CameraWidget,
        layout_parent: Any,
    ) -> None:
        """Exchange two tiles' grid cells."""
        try:
            source_pos = source.grid_position
            target_pos = target.grid_position
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

    def reset_style(self) -> None:
        """Restore border styling and margins after leaving swap mode.

        The 2 px margin after a swap (versus 0 px at start-up) is long-standing
        behaviour; it makes a tile that has been moved look very slightly
        inset and has not bothered anyone in the field, so it is kept.
        """
        self.video_label.setStyleSheet("")
        if self.swap_active:
            self._layout.setContentsMargins(6, 6, 6, 6)
            self.setStyleSheet(self.swap_ready_style)
        else:
            self._layout.setContentsMargins(2, 2, 2, 2)
            self.setStyleSheet(self.normal_style)

    # ------------------------------------------------------------------
    # Fullscreen
    # ------------------------------------------------------------------

    def toggle_fullscreen(self) -> None:
        now = time.time() * 1000.0
        if (now - self._last_fullscreen_toggle_ts) < self.fullscreen_debounce_ms:
            logging.debug("Fullscreen toggle debounced for %s", self.widget_id)
            return
        self._last_fullscreen_toggle_ts = now
        if self.is_fullscreen:
            self.exit_fullscreen()
        else:
            self.go_fullscreen()

    def go_fullscreen(self) -> None:
        # The overlay is sized to the primary screen explicitly because
        # showFullScreen alone is not honoured by every compositor.
        if self.is_fullscreen:
            return
        if self._fs_overlay is None:
            self._fs_overlay = FullscreenOverlay(self.exit_fullscreen)
        screen = QtWidgets.QApplication.primaryScreen()
        if screen:
            self._fs_overlay.setGeometry(screen.geometry())
        self._fs_overlay.showFullScreen()
        self._fs_overlay.raise_()
        self._fs_overlay.activateWindow()
        self.is_fullscreen = True
        if self._latest_frame is None and not self.settings_mode:
            self._render_placeholder(self.placeholder_text or PLACEHOLDER_DISCONNECTED)

    def exit_fullscreen(self) -> None:
        if not self.is_fullscreen:
            return
        if self._fs_overlay:
            self._fs_overlay.hide()
        self.is_fullscreen = False
