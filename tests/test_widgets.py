"""
Tests for ui/widgets.py - Widget lifecycle and fullscreen behavior.
"""

import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


class TestCameraWidgetInit:
    """Test CameraWidget initialization."""

    @pytest.mark.requires_display
    def test_widget_creation_placeholder(self, qapp):
        """Test creating a placeholder widget (no camera)."""
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
        """Test creating a settings tile widget."""
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
    """Test fullscreen enter/exit behavior."""

    @pytest.mark.requires_display
    def test_toggle_fullscreen_enters(self, qapp):
        """Test toggle_fullscreen enters fullscreen when not fullscreen."""
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
        """Test toggle_fullscreen exits fullscreen when fullscreen."""
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
        """Test calling go_fullscreen multiple times is safe."""
        from ui.widgets import CameraWidget
        
        widget = CameraWidget(
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


class TestPermanentFailure:
    """Truth table for is_permanently_failed: limit-logged AND extended
    cooldown elapsed must both hold before a widget is treated as
    permanently failed.
    """

    @pytest.mark.requires_display
    def test_limit_not_logged_is_not_permanently_failed(self, qapp):
        """Restart limit never hit -> never permanently failed, regardless of timing."""
        from ui.widgets import CameraWidget

        widget = CameraWidget(stream_link=None, enable_capture=False)
        widget._restart_limit_logged = False
        widget._last_restart_ts = 0.0

        assert widget.is_permanently_failed(time.time()) is False

        widget.cleanup()

    @pytest.mark.requires_display
    def test_limit_logged_but_cooldown_not_elapsed_is_not_permanently_failed(self, qapp):
        """Limit hit but extended cooldown hasn't passed yet -> not permanently failed."""
        from ui.widgets import CameraWidget

        widget = CameraWidget(stream_link=None, enable_capture=False)
        widget._restart_limit_logged = True
        now = time.time()
        widget._last_restart_ts = now

        assert widget.is_permanently_failed(now) is False

        widget.cleanup()

    @pytest.mark.requires_display
    def test_limit_logged_and_cooldown_elapsed_is_permanently_failed(self, qapp):
        """Limit hit and extended cooldown has passed -> permanently failed."""
        from ui.widgets import CameraWidget

        widget = CameraWidget(stream_link=None, enable_capture=False)
        widget._restart_limit_logged = True
        now = time.time()
        widget._last_restart_ts = now - widget._extended_cooldown_sec

        assert widget.is_permanently_failed(now) is True

        widget.cleanup()


class TestSpawnWorker:
    """Characterization tests pinning CaptureWorker construction/wiring/start
    at each of the three call sites (__init__, attach_camera,
    _restart_capture_if_stale), before/after the _spawn_worker extraction.
    """

    @pytest.mark.requires_display
    def test_init_spawns_and_wires_worker(self, qapp):
        """__init__ constructs, wires, and starts a CaptureWorker when capture is enabled."""
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
        """attach_camera constructs, wires, and starts a new CaptureWorker."""
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
        """_restart_capture_if_stale replaces a stopped worker with a new one,
        reading the previous capture size back off the old worker via getattr.
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
            )
            assert widget.worker is new_worker
            new_worker.frame_ready.connect.assert_called_once_with(widget.on_frame)
            new_worker.status_changed.connect.assert_called_once_with(
                widget.on_status_changed
            )
            new_worker.start.assert_called_once()

            widget.cleanup()


class TestBlitScaled:
    """Characterization tests pinning the scaled-pixmap blit path in
    _render_latest_frame, for both grid and fullscreen targets.
    """

    @pytest.mark.requires_display
    def test_render_latest_frame_blits_grid_pixmap(self, qapp):
        """Feeding a frame produces a pixmap on the grid video_label."""
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
        """Feeding a frame while fullscreen produces a pixmap on the overlay label."""
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
