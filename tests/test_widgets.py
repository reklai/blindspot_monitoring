"""Camera widget state, rendering, watchdog, and worker-lifecycle contracts.

Most cases use capture-disabled widgets or mocked workers so they exercise Qt
behavior without camera hardware or background threads. Dedicated regression
cases use real arrays and one controlled ``QThread`` where object lifetime is
the behavior being protected.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


@pytest.fixture(autouse=True)
def _clear_zombie_workers():
    """Reset the process-wide zombie-worker registry after every test.

    Parking intentionally retains strong references, so mocked workers and the
    reap timer would otherwise leak state into later lifecycle cases.
    """
    yield
    import ui.widgets as widgets_mod

    if hasattr(widgets_mod, "_zombie_workers"):
        widgets_mod._zombie_workers.clear()
    timer = getattr(widgets_mod, "_zombie_reap_timer", None)
    if timer is not None:
        timer.stop()


class TestCameraWidgetInit:
    """The grid's placeholder and settings tiles both remain capture-free."""

    @pytest.mark.requires_display
    def test_widget_creation_placeholder(self, qapp):
        """An empty camera slot retains its label and starts capture-disabled."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
            placeholder_text="TEST",
        )
        
        assert widget.camera_stream_link is None
        assert widget.capture_enabled is False
        assert widget.placeholder_text == "TEST"
        assert not widget.is_fullscreen
        
        widget.cleanup()

    @pytest.mark.requires_display
    def test_widget_creation_settings_mode(self, qapp):
        """The settings tile is distinct from a camera placeholder and never captures."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
            settings_mode=True,
            placeholder_text="SETTINGS",
        )
        
        assert widget.settings_mode is True
        assert widget.capture_enabled is False
        
        widget.cleanup()


class TestFullscreenBehavior:
    """Protect the grid-to-overlay fullscreen state transitions."""

    @pytest.mark.requires_display
    def test_toggle_fullscreen_enters(self, qapp):
        """Entering fullscreen moves the widget into overlay state."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
        )
        
        assert not widget.is_fullscreen
        widget.go_fullscreen()
        assert widget.is_fullscreen
        
        widget.exit_fullscreen()
        widget.cleanup()

    @pytest.mark.requires_display
    def test_toggle_fullscreen_exits(self, qapp):
        """Exiting the overlay restores the non-fullscreen state."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
        )
        
        widget.go_fullscreen()
        assert widget.is_fullscreen
        
        widget.exit_fullscreen()
        assert not widget.is_fullscreen
        
        widget.cleanup()

    @pytest.mark.requires_display
    def test_go_fullscreen_idempotent(self, qapp):
        """A duplicate enter request does not create conflicting overlay state."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
        )
        
        widget.go_fullscreen()
        assert widget.is_fullscreen
        
        # Repeating the transition models duplicate click or key events.
        widget.go_fullscreen()
        assert widget.is_fullscreen
        
        widget.exit_fullscreen()
        widget.cleanup()

    @pytest.mark.requires_display
    def test_exit_fullscreen_idempotent(self, qapp):
        """An exit request is harmless when the widget is already in the grid."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
        )
        
        assert not widget.is_fullscreen
        
        widget.exit_fullscreen()
        assert not widget.is_fullscreen
        
        widget.cleanup()

    @pytest.mark.requires_display
    def test_rapid_fullscreen_toggle(self, qapp):
        """Repeated toggle events leave a valid boolean state and clean up safely."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
        )
        
        # Ten transitions return to the starting state, while still exercising
        # repeated overlay creation and teardown.
        for _ in range(10):
            widget.toggle_fullscreen()
        
        final_state = widget.is_fullscreen
        assert isinstance(final_state, bool)
        
        widget.exit_fullscreen()
        widget.cleanup()


class TestNightMode:
    """Define the user-visible night-mode flag transitions."""

    @pytest.mark.requires_display
    def test_night_mode_default_off(self, qapp):
        """New tiles preserve unmodified color unless night mode is requested."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
        )
        
        assert widget.night_mode_enabled is False
        
        widget.cleanup()

    @pytest.mark.requires_display
    def test_set_night_mode(self, qapp):
        """Night mode can be enabled and subsequently disabled on the same tile."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
        )
        
        widget.set_night_mode(True)
        assert widget.night_mode_enabled is True
        
        widget.set_night_mode(False)
        assert widget.night_mode_enabled is False
        
        widget.cleanup()


class TestWidgetCleanup:
    """Keep cleanup safe for placeholder and repeated teardown paths."""

    @pytest.mark.requires_display
    def test_cleanup_without_worker(self, qapp):
        """A capture-free tile requires no worker-specific teardown."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
        )
        
        widget.cleanup()

    @pytest.mark.requires_display
    def test_cleanup_idempotent(self, qapp):
        """Repeated ownership teardown does not revisit released resources."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
        )
        
        widget.cleanup()
        # Qt owners may invoke explicit cleanup before ``aboutToQuit`` does it again.
        widget.cleanup()


class TestSwapMode:
    """Manual camera swapping starts inactive and can restore normal styling."""

    @pytest.mark.requires_display
    def test_swap_active_default(self, qapp):
        """A new tile does not appear selected for a swap."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
        )
        
        assert widget.swap_active is False
        
        widget.cleanup()

    @pytest.mark.requires_display
    def test_reset_style(self, qapp):
        """Style reset accepts an unselected placeholder without special setup."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
        )
        
        widget.reset_style()
        
        widget.cleanup()


class TestDynamicFPS:
    """Define dynamic capture and render-rate guards at the widget boundary."""

    @pytest.mark.requires_display
    def test_set_dynamic_fps(self, qapp):
        """Capture-rate changes are ignored for placeholders."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
            target_fps=30.0,
        )
        
        # ``enable_capture=False`` deliberately selects the early-return branch.
        widget.set_dynamic_fps(15.0)
        assert widget.current_target_fps == 30.0
        
        widget.cleanup()

    @pytest.mark.requires_display
    def test_set_dynamic_fps_respects_minimum(self, qapp):
        """An active tile clamps capture rate at the configured safe floor."""
        from ui.widgets import CameraWidget
        from core import config
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
            target_fps=30.0,
        )
        
        # Opening a real worker is unnecessary; the flag alone selects active behavior.
        widget.capture_enabled = True
        
        # One FPS is below the supported dynamic range in the default config.
        widget.set_dynamic_fps(1.0)
        assert widget.current_target_fps == config.MIN_DYNAMIC_FPS
        
        widget.cleanup()

    @pytest.mark.requires_display
    def test_set_dynamic_ui_fps(self, qapp):
        """Requested render rates cannot fall below the configured UI floor."""
        from ui.widgets import CameraWidget
        from core import config
        
        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
            ui_fps=15,
        )
        
        # Rendering overhead can adjust the stored value, so the stable contract
        # is the lower bound rather than exact equality.
        widget.set_dynamic_ui_fps(10)
        assert widget.ui_render_fps >= config.MIN_DYNAMIC_UI_FPS

        widget.cleanup()


class TestPermanentFailure:
    """Define when restart exhaustion becomes eligible for detach.

    Both a recorded restart-limit breach and the extended cooldown must hold;
    separating these inputs protects the recovery grace period.
    """

    @pytest.mark.requires_display
    def test_limit_not_logged_is_not_permanently_failed(self, qapp):
        """Elapsed time alone cannot fail a widget that retained restart budget."""
        from ui.widgets import CameraWidget

        widget = CameraWidget(stream_link=None, enable_capture=False)
        widget._restart_limit_logged = False
        widget._last_restart_ts = 0.0

        assert widget.is_permanently_failed(time.time()) is False

        widget.cleanup()

    @pytest.mark.requires_display
    def test_limit_logged_but_cooldown_not_elapsed_is_not_permanently_failed(self, qapp):
        """A restart-limit breach still receives its extended recovery cooldown."""
        from ui.widgets import CameraWidget

        widget = CameraWidget(stream_link=None, enable_capture=False)
        widget._restart_limit_logged = True
        now = time.time()
        widget._last_restart_ts = now

        assert widget.is_permanently_failed(now) is False

        widget.cleanup()

    @pytest.mark.requires_display
    def test_limit_logged_and_cooldown_elapsed_is_permanently_failed(self, qapp):
        """Exhausted budget after the cooldown marks the widget for detachment."""
        from ui.widgets import CameraWidget

        widget = CameraWidget(stream_link=None, enable_capture=False)
        widget._restart_limit_logged = True
        now = time.time()
        widget._last_restart_ts = now - widget._extended_cooldown_sec

        assert widget.is_permanently_failed(now) is True

        widget.cleanup()


class TestSpawnWorker:
    """Keep all worker creation paths on the shared wiring contract.

    Construction, later attachment, and stale recovery must pass equivalent
    capture settings, connect both signals, and start exactly one worker. These
    cases preserve that behavior across refactors of ``_spawn_worker``.
    """

    @pytest.mark.requires_display
    def test_init_spawns_and_wires_worker(self, qapp):
        """An initially populated tile starts a fully connected worker."""
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            mock_worker = MagicMock()
            mock_worker_cls.return_value = mock_worker

            widget = CameraWidget(
                stream_link=0,
                enable_capture=True,
                target_fps=20.0,
                request_capture_size=(640, 480),
            )

            mock_worker_cls.assert_called_once_with(
                0,
                parent=widget,
                target_fps=20.0,
                capture_width=640,
                capture_height=480,
                ui_fps=widget.ui_render_fps,
            )
            assert widget.worker is mock_worker
            mock_worker.frame_ready.connect.assert_called_once_with(widget.on_frame)
            mock_worker.status_changed.connect.assert_called_once_with(
                widget.on_status_changed
            )
            mock_worker.start.assert_called_once()

            widget.cleanup()

    @pytest.mark.requires_display
    def test_attach_camera_spawns_and_wires_worker(self, qapp):
        """Filling a placeholder later uses the same worker wiring as construction."""
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            widget = CameraWidget(
                stream_link=None,
                enable_capture=False,
            )
            mock_worker_cls.reset_mock()
            mock_worker = MagicMock()
            mock_worker_cls.return_value = mock_worker

            widget.attach_camera(
                stream_link=1,
                target_fps=15.0,
                request_capture_size=(320, 240),
            )

            mock_worker_cls.assert_called_once_with(
                1,
                parent=widget,
                target_fps=15.0,
                capture_width=320,
                capture_height=240,
                ui_fps=widget.ui_render_fps,
            )
            assert widget.worker is mock_worker
            mock_worker.frame_ready.connect.assert_called_once_with(widget.on_frame)
            mock_worker.status_changed.connect.assert_called_once_with(
                widget.on_status_changed
            )
            mock_worker.start.assert_called_once()

            widget.cleanup()

    @pytest.mark.requires_display
    def test_restart_capture_if_stale_spawns_and_wires_worker(self, qapp):
        """A clean stale restart carries the old worker's capture size forward.

        Requested dimensions live on the worker after initial construction, so
        replacement must read them before disposing the old instance.
        """
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            old_worker = MagicMock()
            old_worker.isRunning.return_value = False
            old_worker.capture_width = 320
            old_worker.capture_height = 240
            mock_worker_cls.return_value = old_worker

            widget = CameraWidget(
                stream_link=2,
                enable_capture=True,
                target_fps=10.0,
                request_capture_size=(320, 240),
            )

            new_worker = MagicMock()
            mock_worker_cls.reset_mock()
            mock_worker_cls.return_value = new_worker

            widget._restart_capture_if_stale()

            mock_worker_cls.assert_called_once_with(
                2,
                parent=widget,
                target_fps=10.0,
                capture_width=320,
                capture_height=240,
                ui_fps=widget.ui_render_fps,
            )
            assert widget.worker is new_worker
            new_worker.frame_ready.connect.assert_called_once_with(widget.on_frame)
            new_worker.status_changed.connect.assert_called_once_with(
                widget.on_status_changed
            )
            new_worker.start.assert_called_once()

            widget.cleanup()

    @pytest.mark.requires_display
    def test_restart_bail_out_preserves_budget_and_is_detachable(self, qapp):
        """An unkillable worker exits restart logic without spawning another.

        The failed stop is not a completed restart, so it preserves budget. It
        still advances cooldown to prevent a hot loop and leaves the widget
        eligible for eventual detach while the zombie is parked safely.
        """
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            old_worker = MagicMock()
            # ``False`` is the worker's explicit signal that its thread remains alive.
            old_worker.stop.return_value = False
            old_worker.isRunning.return_value = True
            old_worker.capture_width = 320
            old_worker.capture_height = 240
            mock_worker_cls.return_value = old_worker

            widget = CameraWidget(
                stream_link=2,
                enable_capture=True,
                target_fps=10.0,
                request_capture_size=(320, 240),
            )
            # Move beyond the ordinary gate so the stop branch is reached.
            widget._last_restart_ts = 0.0
            events_before = len(widget._restart_events)

            mock_worker_cls.reset_mock()

            now = time.time()
            widget._restart_capture_if_stale()

            # Only a successfully replaced worker should consume the rolling budget.
            assert len(widget._restart_events) == events_before
            # Timestamp advancement prevents each render tick retrying the same wedge.
            assert widget._last_restart_ts >= now
            # Clearing the active reference allows main's detach planner to take over.
            old_worker.stop.assert_called_once()
            assert widget.worker is None
            mock_worker_cls.assert_not_called()
            # Simulate the later sweep boundary without waiting in real time.
            widget._last_restart_ts = now - widget._extended_cooldown_sec
            assert widget.is_permanently_failed(now) is True

            widget.cleanup()

    @pytest.mark.requires_display
    def test_restart_success_consumes_budget(self, qapp):
        """A completed replacement records exactly one restart-budget event.

        This complements the bail-out case so preserving budget there does not
        accidentally make normal restarts unlimited.
        """
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            old_worker = MagicMock()
            old_worker.stop.return_value = True
            old_worker.isRunning.return_value = False
            old_worker.capture_width = 320
            old_worker.capture_height = 240
            mock_worker_cls.return_value = old_worker

            widget = CameraWidget(
                stream_link=2,
                enable_capture=True,
                target_fps=10.0,
                request_capture_size=(320, 240),
            )
            widget._last_restart_ts = 0.0
            events_before = len(widget._restart_events)

            new_worker = MagicMock()
            mock_worker_cls.reset_mock()
            mock_worker_cls.return_value = new_worker

            widget._restart_capture_if_stale()

            assert len(widget._restart_events) == events_before + 1
            assert widget.worker is new_worker
            new_worker.start.assert_called_once()

            widget.cleanup()


class TestEmitRateAlignment:
    """Keep the worker's emission ceiling synchronized with widget rendering."""

    @pytest.mark.requires_display
    def test_spawn_worker_passes_render_rate_as_ui_fps(self, qapp):
        """Worker construction receives both capture rate and render ceiling."""
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            mock_worker_cls.return_value = MagicMock()
            widget = CameraWidget(
                stream_link=0,
                enable_capture=True,
                target_fps=25.0,
                request_capture_size=(640, 480),
                ui_fps=20,
            )
            _, kwargs = mock_worker_cls.call_args
            assert kwargs["ui_fps"] == 20
            assert kwargs["target_fps"] == 25.0
            widget.cleanup()

    @pytest.mark.requires_display
    def test_dynamic_ui_fps_pushes_new_bound_to_worker(self, qapp):
        """A runtime render-rate change immediately updates the active worker."""
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            worker = MagicMock()
            mock_worker_cls.return_value = worker
            widget = CameraWidget(
                stream_link=0,
                enable_capture=True,
                target_fps=25.0,
                request_capture_size=(640, 480),
                ui_fps=20,
            )
            worker.set_ui_fps.reset_mock()

            widget.set_dynamic_ui_fps(12)

            worker.set_ui_fps.assert_called_once_with(widget.ui_render_fps)
            assert widget.ui_render_fps == 12
            widget.cleanup()


class TestBrightnessRendering:
    """Protect non-destructive brightness correction and buffer reuse.

    Rendering applies one lookup table to the whole image while retaining the
    original latest frame. Mutating that source would compound brightness after
    a resize or any other forced re-render of the same frame.
    """

    @pytest.mark.requires_display
    def test_single_lut_matches_independent_numpy_result(self, qapp):
        """Whole-array lookup matches an independent NumPy indexing result."""
        from ui.widgets import CameraWidget

        widget = CameraWidget(stream_link=None, enable_capture=False)
        widget.resize(200, 150)
        widget.show()

        frame = (np.arange(48 * 64 * 3).reshape(48, 64, 3) % 256).astype(np.uint8)
        original = frame.copy()
        widget.on_frame(frame)
        widget.set_brightness(1.5)

        # Fancy indexing supplies an implementation-independent expected array.
        expected = widget._brightness_lut[original]

        widget._render_latest_frame()

        assert widget._brightness_buffer is not None
        assert np.array_equal(widget._brightness_buffer, expected)

        widget.cleanup()

    @pytest.mark.requires_display
    def test_brightness_does_not_mutate_latest_frame(self, qapp):
        """Re-rendering one frame cannot apply brightness a second time."""
        from ui.widgets import CameraWidget

        widget = CameraWidget(stream_link=None, enable_capture=False)
        widget.resize(200, 150)
        widget.show()

        frame = (np.arange(48 * 64 * 3).reshape(48, 64, 3) % 256).astype(np.uint8)
        original = frame.copy()
        widget.on_frame(frame)
        widget.set_brightness(1.5)

        widget._render_latest_frame()
        assert np.array_equal(widget._latest_frame, original)

        # Reset render-cache markers to reproduce the path taken after a resize.
        widget._last_rendered_id = -1
        widget._last_rendered_size = None
        widget._render_latest_frame()
        assert np.array_equal(widget._latest_frame, original)

        widget.cleanup()

    @pytest.mark.requires_display
    def test_brightness_buffer_reused_across_renders(self, qapp):
        """Repeated renders at one resolution reuse the correction buffer."""
        from ui.widgets import CameraWidget

        widget = CameraWidget(stream_link=None, enable_capture=False)
        widget.resize(200, 150)
        widget.show()

        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        widget.on_frame(frame)
        widget.set_brightness(1.5)

        widget._render_latest_frame()
        buf1 = widget._brightness_buffer
        widget._last_rendered_id = -1
        widget._render_latest_frame()
        assert widget._brightness_buffer is buf1

        widget.cleanup()


class TestBlitScaled:
    """Keep scaled-frame output correct for both available render targets."""

    @pytest.mark.requires_display
    def test_render_latest_frame_blits_grid_pixmap(self, qapp):
        """Grid rendering replaces placeholder text with a valid pixmap."""
        from ui.widgets import CameraWidget

        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
        )
        widget.resize(200, 150)
        widget.show()

        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        widget.on_frame(frame)
        widget._render_latest_frame()

        pixmap = widget.video_label.pixmap()
        assert pixmap is not None
        assert not pixmap.isNull()
        assert widget.video_label.text() == ""

        widget.cleanup()

    @pytest.mark.requires_display
    def test_render_latest_frame_blits_fullscreen_pixmap(self, qapp):
        """Fullscreen rendering targets the overlay label rather than the grid label."""
        from ui.widgets import CameraWidget

        widget = CameraWidget(
            stream_link=None,
            enable_capture=False,
        )
        widget.go_fullscreen()

        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        widget.on_frame(frame)
        widget._render_latest_frame()

        pixmap = widget._fs_overlay.label.pixmap()
        assert pixmap is not None
        assert not pixmap.isNull()
        assert widget._fs_overlay.label.text() == ""

        widget.exit_fullscreen()
        widget.cleanup()


class TestFirstFrameWatchdog:
    """A new worker that never delivers its first frame remains recoverable.

    The worker's reconnect loop handles open failures, but cannot progress if
    its first ``grab`` blocks. The widget therefore tracks each attachment
    lifetime and invokes stale recovery after a separate first-frame timeout.
    """

    @pytest.mark.requires_display
    def test_attach_ts_set_on_init(self, qapp):
        """Initial worker creation starts a fresh first-frame timeout window."""
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            mock_worker_cls.return_value = MagicMock()
            before = time.time()
            widget = CameraWidget(stream_link=0, enable_capture=True)
            after = time.time()

            assert before <= widget._attach_ts <= after

            widget.cleanup()

    @pytest.mark.requires_display
    def test_attach_ts_set_on_attach_camera(self, qapp):
        """Filling a placeholder starts its own first-frame timeout window."""
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            widget = CameraWidget(stream_link=None, enable_capture=False)
            widget._attach_ts = 0.0
            mock_worker_cls.return_value = MagicMock()

            before = time.time()
            widget.attach_camera(
                stream_link=1, target_fps=15.0, request_capture_size=(320, 240)
            )
            after = time.time()

            assert before <= widget._attach_ts <= after

            widget.cleanup()

    @pytest.mark.requires_display
    def test_attach_ts_set_on_successful_restart(self, qapp):
        """A replacement worker receives a new grace period for its first frame."""
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            old_worker = MagicMock()
            old_worker.stop.return_value = True
            mock_worker_cls.return_value = old_worker

            widget = CameraWidget(
                stream_link=2,
                enable_capture=True,
                target_fps=10.0,
                request_capture_size=(320, 240),
            )
            widget._last_restart_ts = 0.0
            widget._attach_ts = 0.0
            mock_worker_cls.return_value = MagicMock()

            before = time.time()
            widget._restart_capture_if_stale()
            after = time.time()

            assert before <= widget._attach_ts <= after

            widget.cleanup()

    @pytest.mark.requires_display
    def test_attach_ts_not_bumped_on_restart_bail_out(self, qapp):
        """A failed stop cannot claim that a new attachment lifetime began."""
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            old_worker = MagicMock()
            old_worker.stop.return_value = False
            mock_worker_cls.return_value = old_worker

            widget = CameraWidget(
                stream_link=2,
                enable_capture=True,
                target_fps=10.0,
                request_capture_size=(320, 240),
            )
            widget._last_restart_ts = 0.0
            widget._attach_ts = 123.0

            widget._restart_capture_if_stale()

            assert widget._attach_ts == 123.0

            widget.cleanup()

    @pytest.mark.requires_display
    def test_watchdog_triggers_restart_after_timeout(self, qapp):
        """A worker with no first frame is restarted after its grace period."""
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            mock_worker_cls.return_value = MagicMock()
            widget = CameraWidget(stream_link=0, enable_capture=True)
            widget._attach_ts = time.time() - widget._first_frame_timeout_sec - 1.0

            with patch.object(widget, "_restart_capture_if_stale") as mock_restart:
                widget._render_latest_frame()
                mock_restart.assert_called_once()

            widget.cleanup()

    @pytest.mark.requires_display
    def test_watchdog_does_not_trigger_before_timeout(self, qapp):
        """A newly attached worker receives the full startup grace period."""
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            mock_worker_cls.return_value = MagicMock()
            widget = CameraWidget(stream_link=0, enable_capture=True)
            widget._attach_ts = time.time()

            with patch.object(widget, "_restart_capture_if_stale") as mock_restart:
                widget._render_latest_frame()
                mock_restart.assert_not_called()

            widget.cleanup()

    @pytest.mark.requires_display
    def test_watchdog_not_triggered_after_midrun_offline(self, qapp):
        """A mid-run disconnect remains the worker reconnect loop's responsibility.

        After at least one frame, restarting from every render tick would block
        the GUI during ``stop`` and continually refresh cooldown state. The
        widget watchdog is limited to attachment lifetimes with no frame at all.
        """
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            mock_worker_cls.return_value = MagicMock()
            widget = CameraWidget(stream_link=0, enable_capture=True)
            widget._attach_ts = time.time() - widget._first_frame_timeout_sec - 1.0

            # Deliver once before the offline status to distinguish this from a
            # first-frame wedge; going offline clears the displayed frame.
            widget.on_frame(np.zeros((4, 4, 3), dtype=np.uint8))
            widget.on_status_changed(False)
            assert widget._latest_frame is None

            with patch.object(widget, "_restart_capture_if_stale") as mock_restart:
                widget._render_latest_frame()
                mock_restart.assert_not_called()

            widget.cleanup()

    @pytest.mark.requires_display
    def test_watchdog_fires_when_online_but_never_delivered_a_frame(self, qapp):
        """Online status alone does not disarm first-frame recovery.

        A backend can open and emit ``status_changed(True)`` before blocking in
        ``grab`` or ``retrieve``. Those calls have no read timeout, so receipt
        of an actual frame—not a status transition—is the watchdog boundary.
        """
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            mock_worker_cls.return_value = MagicMock()
            widget = CameraWidget(stream_link=0, enable_capture=True)
            widget._attach_ts = time.time() - widget._first_frame_timeout_sec - 1.0

            # Online status updates timing metadata but deliberately leaves frame
            # receipt false for this attachment.
            widget.on_status_changed(True)
            assert widget._latest_frame is None

            with patch.object(widget, "_restart_capture_if_stale") as mock_restart:
                widget._render_latest_frame()
                mock_restart.assert_called_once()

            widget.cleanup()

    @pytest.mark.requires_display
    def test_watchdog_rearms_on_reattach_after_earlier_frames(self, qapp):
        """Frames from an earlier attachment cannot satisfy a replacement worker."""
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            mock_worker_cls.return_value = MagicMock()
            widget = CameraWidget(stream_link=0, enable_capture=True)
            widget.on_frame(np.zeros((4, 4, 3), dtype=np.uint8))
            widget.detach_camera()

            widget.attach_camera(0, 25.0, (320, 240), ui_fps=10)
            widget._attach_ts = time.time() - widget._first_frame_timeout_sec - 1.0
            widget.on_status_changed(True)

            with patch.object(widget, "_restart_capture_if_stale") as mock_restart:
                widget._render_latest_frame()
                mock_restart.assert_called_once()

            widget.cleanup()

    @pytest.mark.requires_display
    def test_watchdog_ignored_for_settings_tile(self, qapp):
        """The non-camera settings tile bypasses rendering and watchdog work."""
        from ui.widgets import CameraWidget

        widget = CameraWidget(
            stream_link=None, enable_capture=False, settings_mode=True
        )
        # An obviously stale timestamp proves settings mode is the deciding guard.
        widget._attach_ts = 0.0

        with patch.object(widget, "_restart_capture_if_stale") as mock_restart:
            widget._render_latest_frame()
            mock_restart.assert_not_called()

        widget.cleanup()

    @pytest.mark.requires_display
    def test_watchdog_ignored_when_capture_disabled(self, qapp):
        """A tile mid-detach does not restart merely because a worker is referenced."""
        from ui.widgets import CameraWidget

        with patch("ui.widgets.CaptureWorker") as mock_worker_cls:
            mock_worker_cls.return_value = MagicMock()
            widget = CameraWidget(stream_link=0, enable_capture=True)
            widget._attach_ts = 0.0
            widget.capture_enabled = False

            with patch.object(widget, "_restart_capture_if_stale") as mock_restart:
                widget._render_latest_frame()
                mock_restart.assert_not_called()

            widget.cleanup()

    @pytest.mark.requires_display
    def test_watchdog_ignored_when_worker_none(self, qapp):
        """Capture state without a worker is left for attachment planning."""
        from ui.widgets import CameraWidget

        widget = CameraWidget(stream_link=None, enable_capture=False)
        # Open the capture-enabled guard so absence of the worker is isolated.
        widget.capture_enabled = True
        widget._attach_ts = 0.0
        assert widget.worker is None

        with patch.object(widget, "_restart_capture_if_stale") as mock_restart:
            widget._render_latest_frame()
            mock_restart.assert_not_called()

        widget.cleanup()


class TestZombieWorkerDisposal:
    """Protect Qt thread ownership when a capture worker cannot stop.

    Calling ``deleteLater`` on a running ``QThread``—or dropping its final
    Python reference—causes Qt to abort the process when deferred deletion is
    processed. Disposal therefore parks a strong reference and reaps it only
    after ``isRunning`` becomes false.
    """

    @pytest.mark.requires_display
    def test_dispose_running_worker_parks_instead_of_delete(self, qapp):
        """A running worker is retained without scheduling Qt deletion."""
        import ui.widgets as widgets_mod
        from ui.widgets import CameraWidget

        widget = CameraWidget(stream_link=None, enable_capture=False)
        zombie = MagicMock()
        zombie.isRunning.return_value = True

        widget._dispose_worker(zombie)

        zombie.deleteLater.assert_not_called()
        assert zombie in widgets_mod._zombie_workers

        widget.cleanup()

    @pytest.mark.requires_display
    def test_dispose_stopped_worker_deletes_immediately(self, qapp):
        """A confirmed-dead worker follows normal deferred Qt deletion."""
        import ui.widgets as widgets_mod
        from ui.widgets import CameraWidget

        widget = CameraWidget(stream_link=None, enable_capture=False)
        worker = MagicMock()
        worker.isRunning.return_value = False

        widget._dispose_worker(worker)

        worker.deleteLater.assert_called_once()
        assert worker not in widgets_mod._zombie_workers

        widget.cleanup()

    @pytest.mark.requires_display
    def test_park_starts_reap_timer(self, qapp):
        """Parking starts the timer that eventually releases exited workers."""
        import ui.widgets as widgets_mod

        zombie = MagicMock()
        zombie.isRunning.return_value = True

        widgets_mod._park_zombie_worker(zombie)

        assert widgets_mod._zombie_reap_timer is not None
        assert widgets_mod._zombie_reap_timer.isActive()

    @pytest.mark.requires_display
    def test_reap_disposes_only_dead_zombies(self, qapp):
        """One reap pass deletes exited workers and retains live ones."""
        import ui.widgets as widgets_mod

        still_running = MagicMock()
        still_running.isRunning.return_value = True
        now_dead = MagicMock()
        now_dead.isRunning.return_value = False
        widgets_mod._park_zombie_worker(still_running)
        widgets_mod._park_zombie_worker(now_dead)

        widgets_mod._reap_zombie_workers()

        now_dead.deleteLater.assert_called_once()
        assert now_dead not in widgets_mod._zombie_workers
        still_running.deleteLater.assert_not_called()
        assert still_running in widgets_mod._zombie_workers

    @pytest.mark.requires_display
    def test_real_running_qthread_survives_dispose(self, qapp):
        """A real running ``QThread`` survives deferred-event processing.

        This is the process-abort regression test that mocks cannot reproduce:
        the thread must remain parked through Qt's delete queue, then become
        reapable after its controlled exit.
        """
        from PyQt6 import QtCore

        import ui.widgets as widgets_mod
        from ui.widgets import CameraWidget

        class _BlockingThread(QtCore.QThread):
            def __init__(self):
                super().__init__()
                self.release = threading.Event()

            def run(self):
                self.release.wait(10.0)

        widget = CameraWidget(stream_link=None, enable_capture=False)
        thread = _BlockingThread()
        thread.start()
        assert thread.isRunning()

        widget._dispose_worker(thread)
        # The former implementation aborted at this event flush because it had
        # queued deletion of a live thread.
        qapp.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        qapp.processEvents()

        assert thread in widgets_mod._zombie_workers

        thread.release.set()
        assert thread.wait(2000)
        widgets_mod._reap_zombie_workers()
        assert thread not in widgets_mod._zombie_workers

        widget.cleanup()
