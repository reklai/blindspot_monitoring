#!/usr/bin/env python3
"""
Camera Dashboard - Main Application Entry Point

A modular PyQt6 application for displaying multiple camera feeds
with dynamic FPS adjustment, hot-plug support, and fullscreen viewing.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import QTimer

from core import (
    config,
    find_working_cameras,
    get_video_indexes,
    is_system_stressed,
    test_single_camera,
)
from ui import CameraWidget, get_smart_grid
from utils import log_health_summary


def safe_cleanup(widgets: list[CameraWidget], cleaned_flag: list[bool]) -> None:
    """Gracefully stop all camera worker threads."""
    if cleaned_flag[0]:
        return
    cleaned_flag[0] = True
    logging.info("Cleaning all cameras")
    for w in list(widgets):
        try:
            w.cleanup()
        except Exception:
            pass


def main() -> None:
    """Create the UI, discover cameras, and start event loop."""
    # Load and apply configuration
    parser = config.load_config()
    config.apply_config(parser)
    config.configure_logging()

    logging.info("Starting camera grid app")
    logging.info("Config loaded from %s", config.CONFIG_PATH)

    app = QtWidgets.QApplication(sys.argv)

    camera_widgets = []
    all_widgets = []
    placeholder_slots = []

    cleaned_flag = [False]

    # Clean shutdown on Ctrl+C
    def on_sigint(sig, frame):
        QtWidgets.QApplication.quit()

    signal.signal(signal.SIGINT, on_sigint)

    app.setStyle(QtWidgets.QStyleFactory.create("Fusion"))
    app.setStyleSheet("QWidget { background: #2b2b2b; color: #ffffff; }")

    mw = QtWidgets.QMainWindow()
    mw.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint)
    central_widget = QtWidgets.QWidget()
    setattr(central_widget, "selected_camera", None)
    mw.setCentralWidget(central_widget)

    # Show first, then fullscreen (avoids race conditions)
    mw.show()

    def force_fullscreen():
        mw.showFullScreen()
        mw.raise_()
        mw.activateWindow()

    QtCore.QTimer.singleShot(50, force_fullscreen)
    QtCore.QTimer.singleShot(300, force_fullscreen)

    primary_screen = app.primaryScreen()
    screen = (
        primary_screen.availableGeometry()
        if primary_screen
        else QtCore.QRect(0, 0, 1920, 1080)
    )
    working_cameras = find_working_cameras()
    logging.info("Found %d cameras", len(working_cameras))

    known_indexes = set(get_video_indexes())
    active_indexes = set(working_cameras)
    failed_indexes = {idx: time.time() for idx in (known_indexes - active_indexes)}

    layout = QtWidgets.QGridLayout(central_widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)

    def restart_app():
        """Restart the entire process (used by settings tile)."""
        logging.info("Restart requested from settings.")
        safe_cleanup(camera_widgets, cleaned_flag)
        python = sys.executable
        try:
            os.execv(python, [python] + sys.argv)
        except OSError as e:
            logging.error("Failed to restart application: %s", e)
            sys.exit(1)

    night_mode_state = {"enabled": False}

    def toggle_night_mode():
        """Toggle night mode for all camera widgets."""
        night_mode_state["enabled"] = not night_mode_state["enabled"]
        enabled = night_mode_state["enabled"]
        logging.info("Night mode %s", "enabled" if enabled else "disabled")
        for w in all_widgets:
            if hasattr(w, "set_night_mode"):
                w.set_night_mode(enabled)
        settings_tile.set_night_mode_button_label(enabled)

    brightness_state = {"value": 1.0}

    def set_brightness_all(value: int):
        """Set brightness for all camera widgets."""
        brightness_state["value"] = value / 100.0
        logging.info("Brightness %d", value)
        for w in all_widgets:
            if hasattr(w, "set_brightness"):
                w.set_brightness(brightness_state["value"])

    # Settings tile (always present, top-left)
    settings_tile = CameraWidget(
        width=1,
        height=1,
        stream_link=None,
        parent=central_widget,
        target_fps=None,
        request_capture_size=None,
        ui_fps=5,
        enable_capture=False,
        placeholder_text="SETTINGS",
        settings_mode=True,
        on_restart=restart_app,
        on_night_mode_toggle=toggle_night_mode,
        on_brightness_change=set_brightness_all,
    )
    all_widgets.append(settings_tile)

    active_camera_count = max(1, min(len(working_cameras), config.CAMERA_SLOT_COUNT))
    cap_w, cap_h, cap_fps, ui_fps = config.choose_profile(active_camera_count)
    logging.info("Profile: %dx%d @ %d FPS (UI %d FPS)", cap_w, cap_h, cap_fps, ui_fps)

    # Exactly N camera slots at all times (based on config)
    for slot_idx in range(config.CAMERA_SLOT_COUNT):
        if slot_idx < len(working_cameras):
            cam_index = working_cameras[slot_idx]
            cw = CameraWidget(
                1,
                1,
                cam_index,
                parent=central_widget,
                target_fps=cap_fps,
                request_capture_size=(cap_w, cap_h),
                ui_fps=ui_fps,
                enable_capture=True,
            )
            cw.set_night_mode(night_mode_state["enabled"])
            camera_widgets.append(cw)
        else:
            cw = CameraWidget(
                1,
                1,
                stream_link=None,
                parent=central_widget,
                target_fps=None,
                request_capture_size=None,
                ui_fps=5,
                enable_capture=False,
                placeholder_text="DISCONNECTED",
            )
            cw.set_night_mode(night_mode_state["enabled"])
            placeholder_slots.append(cw)
        all_widgets.append(cw)

    rows, cols = get_smart_grid(len(all_widgets))
    widget_width = max(1, screen.width() // cols)
    widget_height = max(1, screen.height() // rows)

    for cw in all_widgets:
        cw.screen_width = widget_width
        cw.screen_height = widget_height

    for i, cw in enumerate(all_widgets):
        row = i // cols
        col = i % cols
        cw.grid_position = (row, col)
        layout.addWidget(cw, row, col)

    for r in range(rows):
        layout.setRowStretch(r, 1)
    for c in range(cols):
        layout.setColumnStretch(c, 1)

    perf_timer = None
    health_timer = None

    def ensure_perf_timer() -> None:
        nonlocal perf_timer
        if perf_timer is None:
            perf_timer = QTimer(mw)
            perf_timer.setInterval(config.PERF_CHECK_INTERVAL_MS)
            perf_timer.timeout.connect(adjust_fps)
            perf_timer.start()
        elif not perf_timer.isActive():
            perf_timer.start()

    # Dynamic FPS adjustment based on system stress
    if config.DYNAMIC_FPS_ENABLED:
        stress_counter = {"stress": 0, "recover": 0}

        def adjust_fps():
            """Lower or restore FPS based on load/temperature."""
            stressed, load_ratio, temp_c = is_system_stressed()

            if stressed:
                stress_counter["stress"] += 1
                stress_counter["recover"] = 0
            else:
                stress_counter["recover"] += 1
                stress_counter["stress"] = 0

            if stress_counter["stress"] >= config.STRESS_HOLD_COUNT:
                for w in camera_widgets:
                    base = w.base_target_fps or 30
                    cur = w.current_target_fps or base
                    new_fps = max(config.MIN_DYNAMIC_FPS, cur - 2)
                    if new_fps < cur:
                        w.set_dynamic_fps(new_fps)
                    # Use widget's base_ui_fps for consistent recovery target
                    cur_ui = w.ui_render_fps or ui_fps
                    new_ui = max(
                        config.MIN_DYNAMIC_UI_FPS, cur_ui - config.UI_FPS_STEP
                    )
                    if new_ui < cur_ui:
                        w.set_dynamic_ui_fps(new_ui)
                stress_counter["stress"] = 0
                logging.info(
                    "Stress detected (load=%s, temp=%s). Lowering FPS.",
                    f"{load_ratio:.2f}" if load_ratio is not None else "n/a",
                    f"{temp_c:.1f}C" if temp_c is not None else "n/a",
                )

            if stress_counter["recover"] >= config.RECOVER_HOLD_COUNT:
                fps_restored = False
                for w in camera_widgets:
                    base = w.base_target_fps or 30
                    cur = w.current_target_fps or base
                    new_fps = min(base, cur + 2)
                    if new_fps > cur:
                        w.set_dynamic_fps(new_fps)
                        fps_restored = True
                    # Restore toward widget's original base_ui_fps, not profile ui_fps
                    base_ui = w.base_ui_fps or ui_fps
                    cur_ui = w.ui_render_fps or base_ui
                    new_ui = min(base_ui, cur_ui + config.UI_FPS_STEP)
                    if new_ui > cur_ui:
                        w.set_dynamic_ui_fps(new_ui)
                        fps_restored = True
                stress_counter["recover"] = 0
                if fps_restored:
                    logging.info("System stable. Restoring FPS.")

        if camera_widgets:
            ensure_perf_timer()

    # Background rescan to attach new cameras to empty slots
    rescan_timer = None
    rescan_executor = ThreadPoolExecutor(max_workers=1)
    rescan_inflight = {"active": False}
    shutdown_state = {"active": False}

    def stop_timers() -> None:
        shutdown_state["active"] = True
        if perf_timer is not None and perf_timer.isActive():
            perf_timer.stop()
        if rescan_timer is not None and rescan_timer.isActive():
            rescan_timer.stop()
        if health_timer is not None and health_timer.isActive():
            health_timer.stop()
        try:
            rescan_executor.shutdown(wait=False)
        except Exception:
            pass

    def _apply_rescan_results(results: list[tuple[int, Optional[int]]]) -> None:
        rescan_inflight["active"] = False
        if shutdown_state["active"]:
            return
        now = time.time()
        for idx, ok in results:
            if not placeholder_slots:
                break
            if ok is not None:
                slot = placeholder_slots.pop(0)
                active_count = min(
                    config.CAMERA_SLOT_COUNT, len(camera_widgets) + 1
                )
                cap_w, cap_h, cap_fps, ui_fps = config.choose_profile(active_count)
                slot.attach_camera(ok, cap_fps, (cap_w, cap_h), ui_fps=ui_fps)
                slot.set_night_mode(night_mode_state["enabled"])
                camera_widgets.append(slot)
                active_indexes.add(ok)
                failed_indexes.pop(ok, None)
                logging.info("Attached camera %d to empty slot", ok)
                if config.DYNAMIC_FPS_ENABLED:
                    ensure_perf_timer()
            else:
                failed_indexes[idx] = now

    def _run_rescan_tests(candidates: list[int]) -> list[tuple[int, Optional[int]]]:
        results: list[tuple[int, Optional[int]]] = []
        for idx in candidates:
            ok = test_single_camera(
                idx,
                retries=2,
                retry_delay=0.15,
                allow_kill=False,
            )
            results.append((idx, ok))
        return results

    def rescan_and_attach():
        """Scan for new cameras and attach them to placeholders."""
        nonlocal rescan_timer
        
        # First, check for cameras that have permanently failed and detach them
        # This converts them back to placeholder slots
        for w in list(camera_widgets):
            if not w.capture_enabled:
                continue
            # Check if widget has exceeded restart limit and is in extended cooldown
            # We detect this by checking if _restart_limit_logged is set
            if getattr(w, '_restart_limit_logged', False):
                # Check if extended cooldown has passed (2x restart window)
                extended_cooldown = w._restart_window_sec * 2
                now = time.time()
                if (now - w._last_restart_ts) >= extended_cooldown:
                    # Camera has been disconnected long enough, detach it
                    detached_idx = w.detach_camera()
                    if detached_idx is not None:
                        camera_widgets.remove(w)
                        placeholder_slots.append(w)
                        active_indexes.discard(detached_idx)
                        failed_indexes[detached_idx] = now
                        logging.info(
                            "Camera %d detached after prolonged failure, slot available for reuse",
                            detached_idx
                        )
                        # Restart rescan timer if it was stopped
                        if rescan_timer is not None and not rescan_timer.isActive():
                            rescan_timer.start()
                            logging.info("Restarted rescan timer for detached camera slot")
        
        if not placeholder_slots:
            # All slots filled, stop the timer
            if rescan_timer is not None and rescan_timer.isActive():
                rescan_timer.stop()
                logging.info("All camera slots filled, stopping rescan timer")
            return

        now = time.time()
        indexes = get_video_indexes()

        candidates = []
        for idx in indexes:
            if idx in active_indexes:
                continue
            last_failed = failed_indexes.get(idx)
            if (
                last_failed
                and (now - last_failed) < config.FAILED_CAMERA_COOLDOWN_SEC
            ):
                continue
            candidates.append(idx)

        if not candidates:
            return
        if rescan_inflight["active"]:
            return

        rescan_inflight["active"] = True
        future = rescan_executor.submit(_run_rescan_tests, candidates)

        def _on_rescan_done(fut) -> None:
            try:
                results = fut.result()
            except Exception:
                logging.exception("Rescan worker failed")
                results = []
            QtCore.QTimer.singleShot(0, lambda: _apply_rescan_results(results))

        future.add_done_callback(_on_rescan_done)

    rescan_timer = QTimer(mw)
    rescan_timer.setInterval(config.RESCAN_INTERVAL_MS)
    rescan_timer.timeout.connect(rescan_and_attach)
    # Always start rescan timer - it handles both attach and detach scenarios
    rescan_timer.start()

    if config.HEALTH_LOG_INTERVAL_SEC > 0:
        health_timer = QTimer(mw)
        health_timer.setInterval(int(config.HEALTH_LOG_INTERVAL_SEC * 1000))
        health_timer.timeout.connect(
            lambda: log_health_summary(
                camera_widgets,
                placeholder_slots,
                active_indexes,
                failed_indexes,
            )
        )
        health_timer.start()

    app.aboutToQuit.connect(lambda: (stop_timers(), safe_cleanup(camera_widgets, cleaned_flag)))

    def quit_handler() -> None:
        stop_timers()
        safe_cleanup(camera_widgets, cleaned_flag)
        app.quit()

    QtGui.QShortcut(QtGui.QKeySequence("q"), mw, quit_handler)

    logging.info("Short click=fullscreen toggle. Hold 400ms=swap mode. Q=quit.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
