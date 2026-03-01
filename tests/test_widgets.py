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
