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
from typing import Any, Optional

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import QTimer

from core import (
    CameraIdentity,
    assign_slots,
    choose_slot_for_identity,
    config,
    discover_camera_identities,
    find_working_camera_identities,
    is_system_stressed,
    probe_group_fallback,
    test_single_camera,
)
from ui import CameraWidget, get_smart_grid
from utils import log_health_summary, set_cloexec_on_device_fds


class _RescanBridge(QtCore.QObject):
    """Marshals rescan probe results from the executor thread to the GUI
    thread.

    QTimer.singleShot(0, ...) called from a future.add_done_callback runs in
    the executor thread, which has no Qt event loop -- the timer NEVER fires
    and the results are silently dropped (leaving rescan_inflight stuck
    True). A cross-thread signal emit is delivered as a queued call on the
    receiver's (GUI) thread instead, which is the supported marshalling
    mechanism.
    """

    results_ready = QtCore.pyqtSignal(object)


def _make_rescan_done_callback(bridge: _RescanBridge) -> Any:
    """Build the future.add_done_callback for a rescan probe: resolve the
    future (a failure logs and becomes an empty result list) and emit the
    results through `bridge` for GUI-thread application."""

    def _on_rescan_done(fut: Any) -> None:
        try:
            results = fut.result()
        except Exception:
            logging.exception("Rescan worker failed")
            results = []
        bridge.results_ready.emit(results)

    return _on_rescan_done


def _plan_rescan_attachments(
    results: list[tuple[CameraIdentity, Optional[int]]],
    free_slot_indexes: list[int],
    pins: dict[int, str],
    last_slot_by_port: dict[str, int],
    active_ports: set[str],
    failed_ports: dict[str, float],
    now: float,
) -> list[tuple[CameraIdentity, int]]:
    """Decide which probed identity attaches to which slot (pure-ish planner).

    Consumes `results` (each a (CameraIdentity, resolved_index_or_None)
    from a rescan probe) and returns the list of (identity, slot_index)
    attachments the Qt side should perform, in order. Bookkeeping is
    applied in place so the closure only has to do the Qt attach:
      - a successful probe that wins a slot: `active_ports.add(port)`,
        `last_slot_by_port[port] = slot`, `failed_ports.pop(port, None)`;
      - a failed probe (index is None): `failed_ports[port] = now`;
      - a successful probe with NO slot available (all free slots are
        pinned to other ports): SKIPPED, NOT failed -- a reserved-slot
        wait is not a failure, so the camera is retried next tick;
      - a port already in `active_ports`: skipped defensively.

    Slot choice per identity goes through `choose_slot_for_identity`
    (pinned-slot > last-slot memory > lowest free unpinned > None). Free
    slots are consumed as they are assigned so a batch never double-books
    a slot.
    """
    free = sorted(free_slot_indexes)
    attachments: list[tuple[CameraIdentity, int]] = []
    for identity, resolved_index in results:
        port = identity.port_path
        if resolved_index is None:
            failed_ports[port] = now
            continue
        if port in active_ports:
            continue
        slot = choose_slot_for_identity(identity, free, pins, last_slot_by_port)
        if slot is None:
            # Only reserved (pinned-to-other-port) slots remain: wait, do
            # not mark failed so this port is retried on the next tick.
            continue
        attachments.append((identity, slot))
        free.remove(slot)
        active_ports.add(port)
        last_slot_by_port[port] = slot
        failed_ports.pop(port, None)
    return attachments


def _run_rescan_tests(
    candidates: list[CameraIdentity],
) -> list[tuple[CameraIdentity, Optional[int]]]:
    """Probe each candidate identity for a working capture node (runs off the
    Qt thread in the rescan executor).

    Cheap single-node fast path first: probe the provisional
    `identity.stream_target`. On failure, fall back to probing the group's
    remaining nodes (`probe_group_fallback` in core.camera) so a camera
    whose capture node isn't its group's lowest can still hot-plug
    reattach. Fallback (`index:N`, device_path None) identities have a
    single node and no group to expand, so they skip the fallback probe.
    Result entries are (identity, resolved_index_or_None) for the planner.
    """
    results: list[tuple[CameraIdentity, Optional[int]]] = []
    for identity in candidates:
        ok = test_single_camera(
            identity.stream_target,
            retries=2,
            retry_delay=0.15,
            allow_kill=False,
        )
        if ok is not None:
            results.append((identity, ok))
            continue
        rebuilt = None
        if identity.device_path is not None:
            rebuilt = probe_group_fallback(
                identity.port_path,
                identity.index,
                retries=2,
                retry_delay=0.15,
                allow_kill=False,
            )
        if rebuilt is not None:
            results.append((rebuilt, rebuilt.index))
        else:
            results.append((identity, None))
    return results


def _plan_detach_sweep(
    camera_widgets: list[CameraWidget], now: float
) -> list[CameraWidget]:
    """Return the capture widgets that have permanently failed and must be
    detached back to placeholder slots on this sweep (pure decision, no Qt).

    A widget qualifies when it still has capture enabled AND
    `is_permanently_failed(now)` is True (restart budget exhausted, or a
    stale restart bailed out on an unkillable/leaked worker). Placeholder
    or already-detached slots (`capture_enabled` False) are never swept.

    Split out from `rescan_and_attach` so the sweep runs UNCONDITIONALLY
    every tick -- the deployed steady state has no free placeholder slots,
    and a widget that leaks its worker after all slots fill has no other
    recovery path, so this decision must not be gated on placeholder state.
    """
    return [
        w for w in camera_widgets if w.capture_enabled and w.is_permanently_failed(now)
    ]


def _step_widget_fps(w: CameraWidget, stress: bool, profile_ui_fps: int) -> bool:
    """Adjust one widget's capture and UI FPS a single step toward the
    stress-relief direction (lower) or the recovery direction (restore
    toward base). Returns True if either FPS value actually changed.
    """
    changed = False
    base = w.base_target_fps or 30
    cur = w.current_target_fps or base
    if stress:
        new_fps = max(config.MIN_DYNAMIC_FPS, cur - 2)
        if new_fps < cur:
            w.set_dynamic_fps(new_fps)
            changed = True
        # Use widget's base_ui_fps for consistent recovery target
        cur_ui = w.ui_render_fps or profile_ui_fps
        new_ui = max(config.MIN_DYNAMIC_UI_FPS, cur_ui - config.UI_FPS_STEP)
        if new_ui < cur_ui:
            w.set_dynamic_ui_fps(new_ui)
            changed = True
    else:
        new_fps = min(base, cur + 2)
        if new_fps > cur:
            w.set_dynamic_fps(new_fps)
            changed = True
        # Restore toward widget's original base_ui_fps, not profile ui_fps
        base_ui = w.base_ui_fps or profile_ui_fps
        cur_ui = w.ui_render_fps or base_ui
        new_ui = min(base_ui, cur_ui + config.UI_FPS_STEP)
        if new_ui > cur_ui:
            w.set_dynamic_ui_fps(new_ui)
            changed = True
    return changed


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


# Set by _handle_shutdown_signal; read by main()'s startup checkpoints.
# QApplication.quit() is a documented no-op before app.exec() starts, so a
# signal delivered during the multi-second startup camera discovery would
# otherwise be silently swallowed and systemd would wait out TimeoutStopSec
# before SIGKILLing the process mid-capture.
_shutdown_requested = {"flag": False}


def _handle_shutdown_signal(sig: int, frame: Optional[Any]) -> None:
    """Signal handler shared by SIGINT and SIGTERM: request a clean Qt
    shutdown so the normal QApplication.quit() -> aboutToQuit ->
    safe_cleanup path runs instead of the process dying mid-capture.

    Also sets `_shutdown_requested` for signals that arrive before the
    event loop is running (see the flag's comment above).
    """
    _shutdown_requested["flag"] = True
    QtWidgets.QApplication.quit()


def _startup_shutdown_check() -> None:
    """Scheduled with QTimer.singleShot(0, ...) right before app.exec();
    runs as the first event of the live loop and re-reads the shutdown flag.

    A plain pre-exec `if flag` check leaves a race: a signal landing between
    that check and the loop actually starting calls quit() while it is still
    a documented no-op, and the flag would never be read again. Re-checking
    from inside the running loop closes the window -- from here on quit()
    works, and any earlier signal left the flag set.
    """
    if _shutdown_requested["flag"]:
        logging.info("Shutdown requested during startup; quitting immediately")
        QtWidgets.QApplication.quit()


def _install_signal_handlers(app: QtWidgets.QApplication) -> None:
    """Register the shared shutdown handler for both SIGINT and SIGTERM.

    SIGINT covers Ctrl+C during interactive use; SIGTERM covers `systemctl
    stop` / service shutdown (KillSignal=SIGTERM in install.sh's unit file)
    so both trigger the same graceful cleanup path rather than SIGTERM
    falling through to the default terminate-without-cleanup behavior.
    """
    del app  # not needed by the handler; kept for a clear, testable call site
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)


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

    # Clean shutdown on Ctrl+C (SIGINT) and `systemctl stop` (SIGTERM).
    _install_signal_handlers(app)

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

    identities = find_working_camera_identities()
    logging.info("Found %d cameras", len(identities))

    # Startup checkpoint: a SIGTERM/SIGINT during the multi-second discovery
    # above could not stop the app via quit() (no event loop yet). Exit now,
    # before any capture workers are spawned.
    if _shutdown_requested["flag"]:
        logging.info("Shutdown requested during startup discovery; exiting")
        return
    slot_assignment = assign_slots(
        identities, config.CAMERA_SLOT_COUNT, config.SLOT_PINS
    )

    # Identity-keyed bookkeeping (all keyed on the stable USB port_path).
    active_ports: set[str] = set()  # port_paths currently attached
    failed_ports: dict[str, float] = {}  # port_path -> last-failed timestamp
    last_slot_by_port: dict[str, int] = {}  # port_path -> slot (reattach memory)
    port_of_widget: dict[CameraWidget, str] = {}  # widget -> port_path

    layout = QtWidgets.QGridLayout(central_widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)

    def restart_app():
        """Restart the entire process (used by settings tile)."""
        logging.info("Restart requested from settings.")
        safe_cleanup(camera_widgets, cleaned_flag)
        # A capture fd leaked by an unkillable worker has no O_CLOEXEC and
        # would survive execv with our SAME pid, keeping the device claimed
        # in the restarted app with no way to reclaim it there
        # (kill_device_holders skips our own PID). Mark such fds
        # close-on-exec so the exec drops them and the camera comes back.
        marked = set_cloexec_on_device_fds()
        if marked:
            logging.warning(
                "Marked %d camera fd(s) close-on-exec before restart", marked
            )
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

    cap_w, cap_h, cap_fps, ui_fps = config.choose_profile()
    logging.info("Profile: %dx%d @ %d FPS (UI %d FPS)", cap_w, cap_h, cap_fps, ui_fps)

    # Exactly N camera slots at all times (based on config). Each slot is
    # bound to its deterministic identity (or reserved/empty placeholder).
    for slot_idx in range(config.CAMERA_SLOT_COUNT):
        identity = slot_assignment[slot_idx]
        if identity is not None:
            cw = CameraWidget(
                identity.stream_target,
                parent=central_widget,
                target_fps=cap_fps,
                request_capture_size=(cap_w, cap_h),
                ui_fps=ui_fps,
                enable_capture=True,
            )
            cw.slot_index = slot_idx
            cw.set_night_mode(night_mode_state["enabled"])
            camera_widgets.append(cw)
            active_ports.add(identity.port_path)
            last_slot_by_port[identity.port_path] = slot_idx
            port_of_widget[cw] = identity.port_path
        else:
            cw = CameraWidget(
                stream_link=None,
                parent=central_widget,
                target_fps=None,
                request_capture_size=None,
                ui_fps=5,
                enable_capture=False,
                placeholder_text="DISCONNECTED",
            )
            cw.slot_index = slot_idx
            cw.set_night_mode(night_mode_state["enabled"])
            placeholder_slots.append(cw)
        all_widgets.append(cw)

    slot_table = {
        slot_idx: (
            slot_assignment[slot_idx].port_path
            if slot_assignment[slot_idx] is not None
            else ("RESERVED" if slot_idx in config.SLOT_PINS else "EMPTY")
        )
        for slot_idx in range(config.CAMERA_SLOT_COUNT)
    }
    logging.info("Slot assignment (slot -> port): %s", slot_table)

    rows, cols = get_smart_grid(len(all_widgets))

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
                    _step_widget_fps(w, stress=True, profile_ui_fps=ui_fps)
                stress_counter["stress"] = 0
                logging.info(
                    "Stress detected (load=%s, temp=%s). Lowering FPS.",
                    f"{load_ratio:.2f}" if load_ratio is not None else "n/a",
                    f"{temp_c:.1f}C" if temp_c is not None else "n/a",
                )

            if stress_counter["recover"] >= config.RECOVER_HOLD_COUNT:
                fps_restored = False
                for w in camera_widgets:
                    if _step_widget_fps(w, stress=False, profile_ui_fps=ui_fps):
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

    def _apply_rescan_results(
        results: list[tuple[CameraIdentity, Optional[int]]]
    ) -> None:
        rescan_inflight["active"] = False
        if shutdown_state["active"]:
            return
        now = time.time()
        free_slot_indexes = sorted(w.slot_index for w in placeholder_slots)
        # Planner mutates active_ports/last_slot_by_port/failed_ports in place
        # and returns which identity attaches to which slot.
        attachments = _plan_rescan_attachments(
            results,
            free_slot_indexes,
            config.SLOT_PINS,
            last_slot_by_port,
            active_ports,
            failed_ports,
            now,
        )
        slot_to_widget = {w.slot_index: w for w in placeholder_slots}
        for identity, slot_index in attachments:
            widget = slot_to_widget.get(slot_index)
            if widget is None:
                # Invariant: unreachable while slot_index values are unique
                # and free_slot_indexes/slot_to_widget are both derived from
                # this same placeholder_slots snapshot -- every slot_index
                # the planner returns was present in slot_to_widget's keys.
                # If this ever became reachable, the planner's bookkeeping
                # (active_ports/last_slot_by_port/failed_ports) has ALREADY
                # been mutated for this identity, so any future change must
                # preserve this invariant rather than relying on this
                # continue as a safety net.
                continue
            placeholder_slots.remove(widget)
            cap_w, cap_h, cap_fps, ui_fps = config.choose_profile()
            widget.attach_camera(
                identity.stream_target, cap_fps, (cap_w, cap_h), ui_fps=ui_fps
            )
            widget.set_night_mode(night_mode_state["enabled"])
            camera_widgets.append(widget)
            port_of_widget[widget] = identity.port_path
            logging.info(
                "Attached camera port %s (/dev/video%d) to slot %d",
                identity.port_path,
                identity.index,
                slot_index,
            )
            if config.DYNAMIC_FPS_ENABLED:
                ensure_perf_timer()

    # Created on the GUI thread (parented to mw) so the cross-thread emit in
    # the rescan done-callback is delivered here as a queued call.
    rescan_bridge = _RescanBridge(mw)
    rescan_bridge.results_ready.connect(_apply_rescan_results)

    def rescan_and_attach():
        """Scan for new cameras and attach them to placeholders.

        The rescan timer runs ALWAYS (never self-stops): the detach sweep
        below is the only recovery path for a permanently-failed/leaked
        widget, and in the deployed steady state all slots are filled, so
        gating either the timer or the sweep on placeholder availability
        would strand such a widget as a dead DISCONNECTED tile until process
        restart. The sweep is a cheap <= slot_count loop every 15s.
        """
        # First, detach any capture widgets that have permanently failed so
        # their slots become placeholders again. Runs regardless of whether
        # any placeholder is currently free (see _plan_detach_sweep).
        now = time.time()
        for w in _plan_detach_sweep(list(camera_widgets), now):
            detached_idx = w.detach_camera()
            if detached_idx is not None:
                camera_widgets.remove(w)
                placeholder_slots.append(w)
                # Reclaim the port; keep last_slot_by_port as reattach
                # memory so a replug returns to this same slot.
                port = port_of_widget.pop(w, None)
                if port is not None:
                    active_ports.discard(port)
                    failed_ports[port] = now
                logging.info(
                    "Camera port %s (was %s) detached from slot %d after "
                    "prolonged failure, slot available for reuse",
                    port,
                    detached_idx,
                    w.slot_index,
                )

        if not placeholder_slots:
            # No free slots to attach into this tick; the sweep above still
            # ran, so leave the timer going and just wait for the next tick.
            return

        now = time.time()
        # Cheap, non-probing discovery: each tick's snapshot carries a
        # freshly resolved index, so a same-port-new-index replug is handled
        # automatically.
        discovered = discover_camera_identities()

        candidates = []
        for identity in discovered:
            port = identity.port_path
            if port in active_ports:
                continue
            last_failed = failed_ports.get(port)
            if (
                last_failed
                and (now - last_failed) < config.FAILED_CAMERA_COOLDOWN_SEC
            ):
                continue
            candidates.append(identity)

        if not candidates:
            return
        if rescan_inflight["active"]:
            return

        rescan_inflight["active"] = True
        future = rescan_executor.submit(_run_rescan_tests, candidates)
        future.add_done_callback(_make_rescan_done_callback(rescan_bridge))

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
                active_ports,
                failed_ports,
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
    # Startup checkpoint: a signal that arrived after discovery but before
    # exec() could not quit() a not-yet-running loop -- and a plain flag
    # check here would itself leave a window between the check and the loop
    # starting. Unconditionally re-check the flag from INSIDE the running
    # loop instead (see _startup_shutdown_check); quitting goes through the
    # normal aboutToQuit -> stop_timers/safe_cleanup path since workers are
    # already spawned here.
    QTimer.singleShot(0, _startup_shutdown_check)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
