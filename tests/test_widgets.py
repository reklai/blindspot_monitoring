"""
Tests for ui/widgets.py - Widget lifecycle and fullscreen behavior.
"""

import time
from unittest.mock import MagicMock, patch

import pytest


class TestCameraWidgetInit:
    """Test CameraWidget initialization."""

    @pytest.mark.requires_display
    def test_widget_creation_placeholder(self, qapp):
        """Test creating a placeholder widget (no camera)."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            width=640,
            height=480,
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
        """Test creating a settings tile widget."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            width=1,
            height=1,
            stream_link=None,
            enable_capture=False,
            settings_mode=True,
            placeholder_text="SETTINGS",
        )
        
        assert widget.settings_mode is True
        assert widget.capture_enabled is False
        
        widget.cleanup()


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

    @pytest.mark.requires_display
    def test_rapid_fullscreen_toggle(self, qapp):
        """Test rapid fullscreen toggling doesn't cause issues."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            width=640,
            height=480,
            stream_link=None,
            enable_capture=False,
        )
        
        # Rapid toggles
        for _ in range(10):
            widget.toggle_fullscreen()
        
        # Should end up in a consistent state (either fullscreen or not)
        final_state = widget.is_fullscreen
        assert isinstance(final_state, bool)
        
        widget.exit_fullscreen()
        widget.cleanup()


class TestNightMode:
    """Test night mode functionality."""

    @pytest.mark.requires_display
    def test_night_mode_default_off(self, qapp):
        """Test night mode is off by default."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            width=640,
            height=480,
            stream_link=None,
            enable_capture=False,
        )
        
        assert widget.night_mode_enabled is False
        
        widget.cleanup()

    @pytest.mark.requires_display
    def test_set_night_mode(self, qapp):
        """Test setting night mode."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            width=640,
            height=480,
            stream_link=None,
            enable_capture=False,
        )
        
        widget.set_night_mode(True)
        assert widget.night_mode_enabled is True
        
        widget.set_night_mode(False)
        assert widget.night_mode_enabled is False
        
        widget.cleanup()


class TestWidgetCleanup:
    """Test widget cleanup and resource release."""

    @pytest.mark.requires_display
    def test_cleanup_without_worker(self, qapp):
        """Test cleanup works when no worker is present."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            width=640,
            height=480,
            stream_link=None,
            enable_capture=False,
        )
        
        # Should not raise
        widget.cleanup()

    @pytest.mark.requires_display
    def test_cleanup_idempotent(self, qapp):
        """Test calling cleanup multiple times is safe."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            width=640,
            height=480,
            stream_link=None,
            enable_capture=False,
        )
        
        widget.cleanup()
        widget.cleanup()  # Second call should not crash


class TestSwapMode:
    """Test camera swap mode behavior."""

    @pytest.mark.requires_display
    def test_swap_active_default(self, qapp):
        """Test swap mode is inactive by default."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            width=640,
            height=480,
            stream_link=None,
            enable_capture=False,
        )
        
        assert widget.swap_active is False
        
        widget.cleanup()

    @pytest.mark.requires_display
    def test_reset_style(self, qapp):
        """Test reset_style restores normal appearance."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            width=640,
            height=480,
            stream_link=None,
            enable_capture=False,
        )
        
        # Should not crash
        widget.reset_style()
        
        widget.cleanup()


class TestDynamicFPS:
    """Test dynamic FPS adjustment."""

    @pytest.mark.requires_display
    def test_set_dynamic_fps(self, qapp):
        """Test setting dynamic FPS (requires capture_enabled=True)."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
            width=640,
            height=480,
            stream_link=None,
            enable_capture=False,
            target_fps=30.0,
        )
        
        # When capture_enabled=False, set_dynamic_fps is a no-op
        # This tests the early return path
        widget.set_dynamic_fps(15.0)
        # FPS remains unchanged because capture is disabled
        assert widget.current_target_fps == 30.0
        
        widget.cleanup()

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

    @pytest.mark.requires_display
    def test_set_dynamic_ui_fps(self, qapp):
        """Test setting dynamic UI FPS."""
        from ui.widgets import CameraWidget
        from core import config
        
        widget = CameraWidget(
            width=640,
            height=480,
            stream_link=None,
            enable_capture=False,
            ui_fps=15,
        )
        
        # UI FPS is adjusted to account for RENDER_OVERHEAD_MS
        # The actual ui_render_fps may differ slightly from the requested value
        widget.set_dynamic_ui_fps(10)
        # Just verify it's at or above minimum
        assert widget.ui_render_fps >= config.MIN_DYNAMIC_UI_FPS
        
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
        stuck.frame_ready.disconnect.assert_not_called()
        assert stuck in widgets._parked_workers
        widgets._parked_workers.remove(stuck)
        tile.worker = None
        tile.cleanup()
