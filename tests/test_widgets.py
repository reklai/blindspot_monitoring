"""
Tests for ui/widgets.py - Widget lifecycle and fullscreen behavior.
"""

import time

import pytest


class TestFullscreenBehavior:
    """Test fullscreen enter/exit behavior."""

    @pytest.mark.requires_display
    def test_toggle_fullscreen_enters(self, qapp):
        """Test toggle_fullscreen enters fullscreen when not fullscreen."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            width=640,
            height=480,
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
        """Test toggle_fullscreen exits fullscreen when fullscreen."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            width=640,
            height=480,
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
        """Test calling go_fullscreen multiple times is safe."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            width=640,
            height=480,
            stream_link=None,
            enable_capture=False,
        )
        
        widget.go_fullscreen()
        assert widget.is_fullscreen
        
        # Calling again should not crash or change state
        widget.go_fullscreen()
        assert widget.is_fullscreen
        
        widget.exit_fullscreen()
        widget.cleanup()

    @pytest.mark.requires_display
    def test_exit_fullscreen_idempotent(self, qapp):
        """Test calling exit_fullscreen multiple times is safe."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            width=640,
            height=480,
            stream_link=None,
            enable_capture=False,
        )
        
        assert not widget.is_fullscreen
        
        # Calling exit when not fullscreen should not crash
        widget.exit_fullscreen()
        assert not widget.is_fullscreen
        
        widget.cleanup()

class TestDynamicFPS:
    """Test dynamic FPS adjustment."""

    @pytest.mark.requires_display
    def test_set_dynamic_fps_respects_minimum(self, qapp):
        """Test dynamic FPS clamps to MIN_DYNAMIC_FPS when value is too low."""
        from ui.widgets import CameraWidget
        from core import config
        
        widget = CameraWidget(
            width=640,
            height=480,
            stream_link=None,
            enable_capture=False,
            target_fps=30.0,
        )
        
        # Simulate an active capture widget so set_dynamic_fps doesn't early-return
        widget.capture_enabled = True
        
        # Try to set below minimum
        widget.set_dynamic_fps(1.0)
        assert widget.current_target_fps == config.MIN_DYNAMIC_FPS
        
        widget.cleanup()

class TestRenderingRegressions:
    """Behaviours pinned after the render-path rewrite."""

    def _tile(self, qapp):
        from ui.widgets import CameraWidget
        tile = CameraWidget(320, 240, stream_link=None, enable_capture=False)
        tile.resize(320, 240)
        tile.show()
        return tile

    @pytest.mark.requires_display
    def test_rerender_does_not_compound_brightness(self, qapp):
        import numpy as np
        tile = self._tile(qapp)
        tile.set_brightness(1.5)
        frame = np.full((48, 64, 3), 100, np.uint8)
        tile.on_frame(frame)
        tile._render_latest_frame()
        first = tile.video_label.pixmap().toImage().pixelColor(5, 5).red()
        tile.resize(200, 150)  # forces a re-render of the same frame
        qapp.processEvents()
        tile._render_latest_frame()
        second = tile.video_label.pixmap().toImage().pixelColor(5, 5).red()
        assert first == second == 150
        assert int(frame[0, 0, 0]) == 100, "source frame must be untouched"
        tile.cleanup()

    @pytest.mark.requires_display
    def test_night_mode_repaints_current_frame(self, qapp):
        import numpy as np
        tile = self._tile(qapp)
        tile.on_frame(np.full((48, 64, 3), 100, np.uint8))
        tile._render_latest_frame()
        before = tile.video_label.pixmap().toImage().pixelColor(5, 5)
        assert (before.red(), before.green(), before.blue()) == (100, 100, 100)
        tile.set_night_mode(True)
        tile._render_latest_frame()
        after = tile.video_label.pixmap().toImage().pixelColor(5, 5)
        assert (after.red(), after.green(), after.blue()) == (160, 0, 0)
        tile.cleanup()

    @pytest.mark.requires_display
    def test_unchanged_frame_is_not_repainted(self, qapp):
        import numpy as np
        from unittest.mock import patch
        tile = self._tile(qapp)
        tile.on_frame(np.zeros((48, 64, 3), np.uint8))
        tile._render_latest_frame()
        with patch.object(tile, "_present") as present:
            tile._render_latest_frame()
            present.assert_not_called()
        tile.cleanup()


class TestStaleRecovery:
    @pytest.mark.requires_display
    def test_stale_frame_restarts_worker_within_budget(self, qapp):
        import numpy as np
        from unittest.mock import MagicMock, patch
        from ui.widgets import CameraWidget
        def fresh_worker(*args, **kwargs):
            worker = MagicMock()
            worker.isRunning.return_value = False
            return worker

        with patch("ui.widgets.CaptureWorker", side_effect=fresh_worker):
            tile = CameraWidget(320, 240, stream_link=3, enable_capture=True, target_fps=20)
            first_worker = tile.worker
            tile.on_frame(np.zeros((8, 8, 3), np.uint8))
            tile._last_frame_ts = time.time() - tile._stale_frame_timeout_sec - 1
            tile._render_latest_frame()
            first_worker.stop.assert_called_once()
            assert tile.worker is not first_worker
            assert tile._latest_frame is None
            assert tile.restart_budget.last_restart_ts is not None
            tile.worker = None
            tile.cleanup()

    @pytest.mark.requires_display
    def test_should_detach_requires_exhaustion_and_no_frames(self, qapp):
        import numpy as np
        from unittest.mock import patch
        from ui.widgets import CameraWidget
        with patch("ui.widgets.CaptureWorker") as worker_cls:
            worker_cls.return_value.isRunning.return_value = False
            tile = CameraWidget(320, 240, stream_link=3, enable_capture=True, target_fps=20)
            now = time.time()
            assert not tile.should_detach(now)
            with patch.object(tile.restart_budget, "is_beyond_extended_cooldown", return_value=True):
                assert tile.should_detach(now)
                tile.on_frame(np.zeros((8, 8, 3), np.uint8))
                assert not tile.should_detach(now), "a tile receiving frames is not detached"
            tile.worker = None
            tile.cleanup()

    @pytest.mark.requires_display
    def test_detach_then_attach_resets_budget(self, qapp):
        from unittest.mock import patch
        from ui.widgets import CameraWidget
        with patch("ui.widgets.CaptureWorker") as worker_cls:
            worker_cls.return_value.isRunning.return_value = False
            tile = CameraWidget(320, 240, stream_link=3, enable_capture=True, target_fps=20)
            tile.restart_budget.request(time.time())
            assert tile.detach_camera() == 3
            assert tile.restart_budget.last_restart_ts is None
            assert not tile.capture_enabled and tile.worker is None
            tile.attach_camera(4, 20, (640, 480), ui_fps=15)
            assert tile.capture_enabled and tile.camera_stream_link == 4
            assert tile.base_ui_fps == 15
            tile.worker = None
            tile.cleanup()


class TestSettingsControls:
    @pytest.mark.requires_display
    def test_buttons_fire_callbacks_and_highlight(self, qapp):
        from PyQt6 import QtCore, QtGui
        from ui.widgets import CameraWidget, SettingsControls
        calls = []
        tile = CameraWidget(
            1, 1, stream_link=None, enable_capture=False, settings_mode=True,
            on_restart=lambda: calls.append("restart"),
            on_night_mode_toggle=lambda: calls.append("night"),
            on_brightness_change=lambda v: calls.append(("brightness", v)),
        )
        controls = tile._settings
        release = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseButtonRelease, QtCore.QPointF(1, 1),
            QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        for button in list(controls._actions):
            controls.eventFilter(button, release)
        assert "restart" in calls and "night" in calls
        assert ("brightness", 150) in calls
        assert controls._brightness_buttons[150].styleSheet() == SettingsControls.SELECTED_STYLE
        assert controls._brightness_buttons[100].styleSheet() == SettingsControls.BUTTON_STYLE

        tile.set_night_mode_button_label(True)
        assert controls.night_mode_button.text() == "Nightmode: On"
        tile.cleanup()


class TestUnstoppableWorker:
    @pytest.mark.requires_display
    def test_worker_that_will_not_stop_is_parked_not_deleted(self, qapp):
        from unittest.mock import MagicMock, patch
        from ui import widgets
        from ui.widgets import CameraWidget
        stuck = MagicMock()
        stuck.isRunning.return_value = True
        with patch("ui.widgets.CaptureWorker", return_value=stuck):
            tile = CameraWidget(320, 240, stream_link=3, enable_capture=True, target_fps=20)
            assert tile._retire_worker(stuck) is False
        stuck.stop.assert_called_once()
        stuck.setParent.assert_called_once_with(None)
        stuck.deleteLater.assert_not_called()
        # Disconnected even though it is still running: if the driver call it
        # is stuck in ever returns, its frames must not land on this tile.
        stuck.frame_ready.disconnect.assert_called_once_with(tile.on_frame)
        stuck.status_changed.disconnect.assert_called_once_with(tile.on_status_changed)
        assert stuck in widgets._parked_workers
        widgets._parked_workers.remove(stuck)
        tile.worker = None
        tile.cleanup()


class TestRestartBudgetEndToEnd:
    """Drive the real budget through the tile with a fake clock: three stale
    restarts, a refused fourth, then detach eligibility after the extended
    cooldown."""

    @pytest.mark.requires_display
    def test_stale_restarts_exhaust_then_detach_becomes_eligible(self, qapp, save_restore_config):
        import numpy as np
        from unittest.mock import MagicMock, patch
        from core import config
        from ui.widgets import CameraWidget

        config.STALE_FRAME_TIMEOUT_SEC = 1.0
        config.RESTART_COOLDOWN_SEC = 5.0
        config.RESTART_WINDOW_SEC = 30.0
        config.MAX_RESTARTS_PER_WINDOW = 3

        def fresh_worker(*args, **kwargs):
            w = MagicMock()
            w.isRunning.return_value = False
            return w

        clock = {"t": 1000.0}
        with patch("ui.widgets.CaptureWorker", side_effect=fresh_worker), \
             patch("ui.widgets.time.time", side_effect=lambda: clock["t"]):
            tile = CameraWidget(320, 240, stream_link=3, enable_capture=True, target_fps=20)
            workers = [tile.worker]

            def frame_then_stall():
                tile.on_frame(np.zeros((8, 8, 3), np.uint8))   # stamps _last_frame_ts = now
                clock["t"] += 6.0                              # past cooldown and stale timeout
                tile._render_latest_frame()                    # stale -> restart request

            for _ in range(3):
                frame_then_stall()
                assert tile.worker is not workers[-1], "restart should have replaced the worker"
                workers.append(tile.worker)
            assert not tile.restart_budget.exhausted

            frame_then_stall()  # fourth inside the 30 s window: refused
            assert tile.worker is workers[-1]
            assert tile.restart_budget.exhausted
            assert not tile.should_detach(clock["t"])

            clock["t"] = tile.restart_budget.last_restart_ts + 59.9
            assert not tile.should_detach(clock["t"])
            clock["t"] += 0.2
            assert tile.should_detach(clock["t"]), "no frame since exhaustion + 60 s elapsed"

            tile.on_frame(np.zeros((8, 8, 3), np.uint8))
            assert not tile.should_detach(clock["t"]), "a frame arriving cancels detach"
            tile.worker = None
            tile.cleanup()
