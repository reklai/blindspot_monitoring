#!/usr/bin/env python3
"""
Camera Dashboard - application entry point.

``main()`` loads configuration, discovers cameras, builds a ``Dashboard``
and runs the Qt event loop. ``Dashboard`` owns everything with a lifetime:
the tiles, the grid, the periodic timers (dynamic FPS, hot-plug rescan,
health log) and the single background thread that probes new devices.

Nothing in this module touches a camera directly; that is ``core.camera``.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from enum import Enum, auto
from typing import Optional

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import QTimer, pyqtSignal, pyqtSlot

from core import (
    config,
    find_working_cameras,
    get_video_indexes,
    is_system_stressed,
    test_single_camera,
)
from ui import CameraWidget, get_smart_grid
from utils import log_health_summary

#: Render rate for tiles that never show video (settings, empty slots).
#: They still tick so a placeholder repaints after a fullscreen toggle or a
#: swap, but 5 Hz keeps that cost invisible.
IDLE_TILE_UI_FPS = 5
#: Capture-rate step applied per stress/recovery decision. The UI step is
#: configurable (UI_FPS_STEP); this one never needed to be.
CAPTURE_FPS_STEP = 2


class StressAction(Enum):
    NONE = auto()
    LOWER = auto()
    RESTORE = auto()


class StressController:
    """Hysteresis over the stressed/calm signal.

    Emits LOWER after ``stress_hold`` consecutive stressed samples and
    RESTORE after ``recover_hold`` consecutive calm ones, then starts
    counting again, so each action is applied once per run of samples.
    """

    def __init__(self, stress_hold: int, recover_hold: int) -> None:
        self.stress_hold = max(1, int(stress_hold))
        self.recover_hold = max(1, int(recover_hold))
        self._stress = 0
        self._recover = 0

    def observe(self, stressed: bool) -> StressAction:
        if stressed:
            self._stress += 1
            self._recover = 0
            if self._stress >= self.stress_hold:
                self._stress = 0
                return StressAction.LOWER
        else:
            self._recover += 1
            self._stress = 0
            if self._recover >= self.recover_hold:
                self._recover = 0
                return StressAction.RESTORE
        return StressAction.NONE


ProbeResults = list[tuple[int, Optional[int]]]


def _probe_candidates(candidates: list[int]) -> ProbeResults:
    """Probe each index once without evicting holders. Runs off the UI thread.

    Eviction (killing whatever holds the device) is a boot-time measure. At
    runtime the holder could be one of our own workers mid-reconnect, so the
    rescan only takes devices that open cleanly.
    """
    return [
        (idx, test_single_camera(idx, retries=2, retry_delay=0.15, allow_kill=False))
        for idx in candidates
    ]


class Dashboard(QtCore.QObject):
    """The running application: window, tiles, timers, and their state.

    Every method runs on the UI thread except ``_on_rescan_done``, which the
    probe executor calls on its own thread and which must touch nothing but
    the shutdown flag and the signal. It is a QObject only so it can own
    that signal.
    """

    # Carries probe results from the executor thread to the UI thread. A
    # queued signal is the one cross-thread mechanism Qt guarantees here;
    # QTimer.singleShot from a non-Qt thread never fires.
    _rescan_finished = pyqtSignal(object)

    def __init__(
        self,
        app: QtWidgets.QApplication,
        screen: QtCore.QRect,
        working_cameras: list[int],
        known_indexes: set[int],
    ) -> None:
        super().__init__()
        self.app = app
        self.camera_widgets: list[CameraWidget] = []
        self.placeholder_slots: list[CameraWidget] = []
        self.all_widgets: list[CameraWidget] = []
        # active_indexes: device indexes currently bound to a tile.
        # failed_indexes: index -> time of last failed probe. The rescan skips
        # an index until FAILED_CAMERA_COOLDOWN_SEC has passed, so a dead
        # metadata node or a flaky camera is not re-probed every tick.
        self.active_indexes: set[int] = set(working_cameras)
        self.failed_indexes: dict[int, float] = {
            idx: time.time() for idx in known_indexes - self.active_indexes
        }
        self.night_mode = False
        self.brightness = 1.0
        self._cleaned = False
        self._shutting_down = False

        self.window = QtWidgets.QMainWindow()
        self.window.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint)
        self.central = QtWidgets.QWidget()
        # Swap mode's shared register: the tile currently selected by a long
        # press, or None. Tiles reach it through parent(), which keeps them
        # ignorant of the Dashboard. See CameraWidget._handle_release_as_left_click.
        setattr(self.central, "selected_camera", None)
        self.window.setCentralWidget(self.central)

        self._build_tiles(working_cameras)
        self._lay_out_grid(screen)

        # The perf timer only runs while at least one camera is attached;
        # _apply_rescan_results starts it when the first camera hot-plugs in.
        self.perf_timer: Optional[QTimer] = None
        self.stress = StressController(config.STRESS_HOLD_COUNT, config.RECOVER_HOLD_COUNT)
        if config.DYNAMIC_FPS_ENABLED and self.camera_widgets:
            self._ensure_perf_timer()

        # One probe at a time: opening a V4L2 device can block for seconds,
        # and two probes racing for the same node would fight each other.
        self._rescan_executor = ThreadPoolExecutor(max_workers=1)
        self._rescan_inflight = False
        self._slots_full_logged = False
        self._rescan_finished.connect(self._apply_rescan_results)
        self.rescan_timer = QTimer(self.window)
        self.rescan_timer.setInterval(config.RESCAN_INTERVAL_MS)
        self.rescan_timer.timeout.connect(self.rescan_and_attach)
        self.rescan_timer.start()

        self.health_timer: Optional[QTimer] = None
        if config.HEALTH_LOG_INTERVAL_SEC > 0:
            self.health_timer = QTimer(self.window)
            self.health_timer.setInterval(int(config.HEALTH_LOG_INTERVAL_SEC * 1000))
            self.health_timer.timeout.connect(self._log_health)
            self.health_timer.start()

        app.aboutToQuit.connect(self.shutdown)
        QtGui.QShortcut(QtGui.QKeySequence("q"), self.window, self.quit)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_tiles(self, working_cameras: list[int]) -> None:
        self.settings_tile = CameraWidget(
            width=1,
            height=1,
            stream_link=None,
            parent=self.central,
            target_fps=None,
            request_capture_size=None,
            ui_fps=IDLE_TILE_UI_FPS,
            enable_capture=False,
            placeholder_text="SETTINGS",
            settings_mode=True,
            on_restart=self.restart_app,
            on_night_mode_toggle=self.toggle_night_mode,
            on_brightness_change=self.set_brightness_all,
        )
        self.all_widgets.append(self.settings_tile)

        # choose_profile ignores the count today (the profile is exactly what
        # config.ini says); the argument is kept so a count-aware profile can
        # be reintroduced without touching call sites.
        active_count = max(1, min(len(working_cameras), config.CAMERA_SLOT_COUNT))
        cap_w, cap_h, cap_fps, ui_fps = config.choose_profile(active_count)
        logging.info("Profile: %dx%d @ %d FPS (UI %d FPS)", cap_w, cap_h, cap_fps, ui_fps)

        # Exactly CAMERA_SLOT_COUNT camera tiles, filled left to right.
        for slot_idx in range(config.CAMERA_SLOT_COUNT):
            if slot_idx < len(working_cameras):
                tile = CameraWidget(
                    1,
                    1,
                    working_cameras[slot_idx],
                    parent=self.central,
                    target_fps=cap_fps,
                    request_capture_size=(cap_w, cap_h),
                    ui_fps=ui_fps,
                    enable_capture=True,
                )
                self.camera_widgets.append(tile)
            else:
                tile = CameraWidget(
                    1,
                    1,
                    stream_link=None,
                    parent=self.central,
                    target_fps=None,
                    request_capture_size=None,
                    ui_fps=IDLE_TILE_UI_FPS,
                    enable_capture=False,
                    placeholder_text="DISCONNECTED",
                )
                self.placeholder_slots.append(tile)
            tile.set_night_mode(self.night_mode)
            self.all_widgets.append(tile)

    def _lay_out_grid(self, screen: QtCore.QRect) -> None:
        layout = QtWidgets.QGridLayout(self.central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # The grid counts the settings tile: 3 camera slots + settings = 4
        # tiles = a 2x2 grid. screen_width/height are hints only; the layout
        # stretch factors below are what actually size the tiles.
        rows, cols = get_smart_grid(len(self.all_widgets))
        tile_w = max(1, screen.width() // cols)
        tile_h = max(1, screen.height() // rows)
        for i, tile in enumerate(self.all_widgets):
            tile.screen_width = tile_w
            tile.screen_height = tile_h
            tile.grid_position = (i // cols, i % cols)
            layout.addWidget(tile, *tile.grid_position)
        for r in range(rows):
            layout.setRowStretch(r, 1)
        for c in range(cols):
            layout.setColumnStretch(c, 1)

    def show(self) -> None:
        # Show first, then go fullscreen; some compositors ignore an
        # immediate showFullScreen, hence the delayed retries.
        self.window.show()

        def force_fullscreen() -> None:
            self.window.showFullScreen()
            self.window.raise_()
            self.window.activateWindow()

        QTimer.singleShot(50, force_fullscreen)
        QTimer.singleShot(300, force_fullscreen)

    # ------------------------------------------------------------------
    # Settings tile actions
    # ------------------------------------------------------------------

    def toggle_night_mode(self) -> None:
        self.night_mode = not self.night_mode
        logging.info("Night mode %s", "enabled" if self.night_mode else "disabled")
        for tile in self.all_widgets:
            tile.set_night_mode(self.night_mode)
        self.settings_tile.set_night_mode_button_label(self.night_mode)

    def set_brightness_all(self, percent: int) -> None:
        self.brightness = percent / 100.0
        logging.info("Brightness %d", percent)
        for tile in self.all_widgets:
            tile.set_brightness(self.brightness)

    def restart_app(self) -> None:
        """Replace this process with a fresh one (settings tile 'Restart').

        execv keeps the PID, so systemd sees nothing happen. Capture threads
        are stopped first because an open V4L2 descriptor is inherited across
        exec and would make the new process's discovery pass find its own
        cameras busy.
        """
        logging.info("Restart requested from settings.")
        self.shutdown()
        python = sys.executable
        try:
            os.execv(python, [python] + sys.argv)
        except OSError as exc:
            logging.error("Failed to restart application: %s", exc)
            sys.exit(1)

    # ------------------------------------------------------------------
    # Dynamic FPS
    # ------------------------------------------------------------------

    def _ensure_perf_timer(self) -> None:
        if self.perf_timer is None:
            self.perf_timer = QTimer(self.window)
            self.perf_timer.setInterval(config.PERF_CHECK_INTERVAL_MS)
            self.perf_timer.timeout.connect(self.adjust_fps)
        if not self.perf_timer.isActive():
            self.perf_timer.start()

    def adjust_fps(self) -> None:
        """Lower or restore capture and render rates based on load/temperature."""
        stressed, load_ratio, temp_c = is_system_stressed()
        action = self.stress.observe(stressed)
        if action is StressAction.LOWER:
            self._step_fps(-1)
            logging.info(
                "Stress detected (load=%s, temp=%s). Lowering FPS.",
                f"{load_ratio:.2f}" if load_ratio is not None else "n/a",
                f"{temp_c:.1f}C" if temp_c is not None else "n/a",
            )
        elif action is StressAction.RESTORE:
            if self._step_fps(+1):
                logging.info("System stable. Restoring FPS.")

    def _step_fps(self, direction: int) -> bool:
        """Move every camera tile one step down (-1) or up (+1). Returns True if any changed.

        Each tile remembers its base (profile) rates and its current
        (adjusted) rates. Lowering clamps at the configured floors; restoring
        climbs back toward the base, never above it. Capture and UI rates step
        independently, so one can already be at its limit while the other
        still moves.
        """
        _, _, _, profile_ui_fps = config.choose_profile(len(self.camera_widgets))
        changed = False
        for tile in self.camera_widgets:
            base = tile.base_target_fps or 30
            cur = tile.current_target_fps or base
            if direction < 0:
                new_fps = max(config.MIN_DYNAMIC_FPS, cur - CAPTURE_FPS_STEP)
            else:
                new_fps = min(base, cur + CAPTURE_FPS_STEP)
            if new_fps != cur:
                tile.set_dynamic_fps(new_fps)
                changed = True

            base_ui = tile.base_ui_fps or profile_ui_fps
            cur_ui = tile.ui_render_fps or base_ui
            if direction < 0:
                new_ui = max(config.MIN_DYNAMIC_UI_FPS, cur_ui - config.UI_FPS_STEP)
            else:
                new_ui = min(base_ui, cur_ui + config.UI_FPS_STEP)
            if new_ui != cur_ui:
                tile.set_dynamic_ui_fps(new_ui)
                changed = True
        return changed

    # ------------------------------------------------------------------
    # Hot-plug rescan
    # ------------------------------------------------------------------

    def rescan_and_attach(self) -> None:
        """Periodic: free slots of cameras that gave up, probe new devices.

        Runs every RESCAN_INTERVAL_MS for the life of the process, even when
        every slot is filled, because the detach check has to keep running
        for a camera that fails later.
        """
        now = time.time()
        self._detach_failed_cameras(now)

        if not self.placeholder_slots:
            if not self._slots_full_logged:
                logging.info("All camera slots filled")
                self._slots_full_logged = True
            return
        self._slots_full_logged = False

        if self._rescan_inflight:
            return
        candidates = self._rescan_candidates(now)
        if not candidates:
            return

        self._rescan_inflight = True
        future = self._rescan_executor.submit(_probe_candidates, candidates)
        future.add_done_callback(self._on_rescan_done)

    def _detach_failed_cameras(self, now: float) -> None:
        for tile in list(self.camera_widgets):
            if not tile.should_detach(now):
                continue
            idx = tile.detach_camera()
            if idx is None:
                continue
            self.camera_widgets.remove(tile)
            self.placeholder_slots.append(tile)
            self.active_indexes.discard(idx)
            self.failed_indexes[idx] = now
            logging.info(
                "Camera %d detached after prolonged failure, slot available for reuse", idx
            )

    def _rescan_candidates(self, now: float) -> list[int]:
        candidates = []
        for idx in get_video_indexes():
            if idx in self.active_indexes:
                continue
            last_failed = self.failed_indexes.get(idx)
            if last_failed and (now - last_failed) < config.FAILED_CAMERA_COOLDOWN_SEC:
                continue
            candidates.append(idx)
        return candidates

    def _on_rescan_done(self, future: Future) -> None:
        # Executor thread. Hand the results to the UI thread via the signal.
        try:
            results = future.result()
        except Exception:
            logging.exception("Rescan worker failed")
            results = []
        if self._shutting_down:
            return  # the receiver may be mid-teardown; nothing to attach to
        self._rescan_finished.emit(results)

    @pyqtSlot(object)
    def _apply_rescan_results(self, results: ProbeResults) -> None:
        # UI thread. Slots are handed out in grid order (pop(0)), so a camera
        # that comes back after a detach lands in the first free tile, not
        # necessarily the one it left.
        self._rescan_inflight = False
        if self._shutting_down:
            return
        now = time.time()
        for idx, ok in results:
            if ok is None:
                self.failed_indexes[idx] = now
                continue
            if not self.placeholder_slots:
                break
            slot = self.placeholder_slots.pop(0)
            active_count = min(config.CAMERA_SLOT_COUNT, len(self.camera_widgets) + 1)
            cap_w, cap_h, cap_fps, ui_fps = config.choose_profile(active_count)
            slot.attach_camera(ok, cap_fps, (cap_w, cap_h), ui_fps=ui_fps)
            slot.set_night_mode(self.night_mode)
            slot.set_brightness(self.brightness)
            self.camera_widgets.append(slot)
            self.active_indexes.add(ok)
            self.failed_indexes.pop(ok, None)
            logging.info("Attached camera %d to empty slot", ok)
            if config.DYNAMIC_FPS_ENABLED:
                self._ensure_perf_timer()

    # ------------------------------------------------------------------
    # Health and shutdown
    # ------------------------------------------------------------------

    def _log_health(self) -> None:
        log_health_summary(
            self.camera_widgets,
            self.placeholder_slots,
            self.active_indexes,
            self.failed_indexes,
        )

    def stop_timers(self) -> None:
        self._shutting_down = True
        for timer in (self.perf_timer, self.rescan_timer, self.health_timer):
            if timer is not None and timer.isActive():
                timer.stop()
        self._rescan_executor.shutdown(wait=False)

    def shutdown(self) -> None:
        """Stop timers and every capture thread. Idempotent."""
        if self._cleaned:
            return
        self._cleaned = True
        self.stop_timers()
        logging.info("Cleaning all cameras")
        for tile in list(self.camera_widgets):
            try:
                tile.cleanup()
            except Exception:
                logging.debug("cleanup failed", exc_info=True)

    def quit(self) -> None:
        self.shutdown()
        self.app.quit()


def main() -> None:
    parser = config.load_config()
    config.apply_config(parser)
    config.configure_logging()

    logging.info("Starting camera grid app")
    logging.info("Config loaded from %s", config.CONFIG_PATH)

    app = QtWidgets.QApplication(sys.argv)
    app.setStyle(QtWidgets.QStyleFactory.create("Fusion"))
    app.setStyleSheet("QWidget { background: #2b2b2b; color: #ffffff; }")

    # Python only runs signal handlers between bytecodes; the periodic
    # timers guarantee the interpreter gets control often enough.
    signal.signal(signal.SIGINT, lambda *_: QtWidgets.QApplication.quit())
    # SIGTERM (systemd stop) is left at its default: the process exits at
    # once and the kernel closes the device handles. Quitting through Qt
    # would be tidier but adds a shutdown path that can wedge on a stuck
    # driver, which is worse for a service that systemd restarts anyway.

    primary = app.primaryScreen()
    screen = primary.availableGeometry() if primary else QtCore.QRect(0, 0, 1920, 1080)

    working_cameras = find_working_cameras()
    logging.info("Found %d cameras", len(working_cameras))
    known_indexes = set(get_video_indexes())

    dashboard = Dashboard(app, screen, working_cameras, known_indexes)
    dashboard.show()

    logging.info("Short click=fullscreen toggle. Hold 400ms=swap mode. Q=quit.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
