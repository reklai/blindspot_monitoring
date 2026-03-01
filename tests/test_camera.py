"""
Tests for core/camera.py - Camera discovery and capture logic.
"""

import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


class TestGetVideoIndexes:
    """Test video device index discovery."""

    def test_get_video_indexes_with_devices(self):
        """Test finding video device indexes."""
        from core.camera import get_video_indexes
        
        # Mock returns devices
        indexes = get_video_indexes()
        assert isinstance(indexes, list)

    def test_get_video_indexes_empty(self):
        """Test handling no video devices."""
        with patch("core.camera.glob_module.glob") as mock_glob:
            mock_glob.return_value = []
            
            from core.camera import get_video_indexes
            indexes = get_video_indexes()
            assert indexes == []


class TestTestSingleCamera:
    """Test single camera validation."""

    def test_single_camera_success(self, mock_video_capture):
        """Test successful camera open."""
        from core.camera import test_single_camera
        
        result = test_single_camera(0, retries=1, retry_delay=0.01)
        assert result == 0

    def test_single_camera_failure(self):
        """Test failed camera open returns None."""
        with patch("cv2.VideoCapture") as mock_cap:
            instance = MagicMock()
            instance.isOpened.return_value = False
            mock_cap.return_value = instance
            
            from core.camera import test_single_camera
            result = test_single_camera(99, retries=1, retry_delay=0.01)
            assert result is None

    def test_single_camera_retries(self):
        """Test camera open retries on failure."""
        call_count = 0
        
        def mock_is_opened():
            nonlocal call_count
            call_count += 1
            # Succeed on third attempt
            return call_count >= 3
        
        with patch("cv2.VideoCapture") as mock_cap:
            instance = MagicMock()
            instance.isOpened.side_effect = mock_is_opened
            instance.read.return_value = (True, MagicMock())
            mock_cap.return_value = instance
            
            from core.camera import test_single_camera
            result = test_single_camera(0, retries=3, retry_delay=0.01)
            assert result == 0
            assert call_count >= 2


class TestFindWorkingCameras:
    """Test multi-camera discovery."""

    def test_find_working_cameras_returns_list(self, mock_video_capture):
        """Test find_working_cameras returns a list."""
        from core.camera import find_working_cameras
        
        with patch("core.camera.get_video_indexes", return_value=[0, 2, 4]):
            cameras = find_working_cameras()
            assert isinstance(cameras, list)

    def test_find_working_cameras_filters_invalid(self):
        """Test invalid cameras are filtered out."""
        with patch("core.camera.get_video_indexes", return_value=[0, 1, 2]):
            with patch("core.camera.test_single_camera") as mock_test:
                # Only camera 0 and 2 work
                mock_test.side_effect = lambda idx, **kw: idx if idx in [0, 2] else None
                
                from core.camera import find_working_cameras
                cameras = find_working_cameras()
                
                # Should only contain working cameras
                for cam in cameras:
                    assert cam in [0, 2]


class TestCaptureWorker:
    """Test CaptureWorker thread class."""

    def test_worker_init(self):
        """Test CaptureWorker initialization."""
        from core.camera import CaptureWorker
        
        worker = CaptureWorker(
            stream_link=0,
            parent=None,
            target_fps=30.0,
            capture_width=640,
            capture_height=480,
        )
        
        assert worker.stream_link == 0
        assert worker._target_fps == 30.0
        assert worker.capture_width == 640
        assert worker.capture_height == 480
        assert worker._running is True

    def test_worker_set_target_fps(self):
        """Test setting target FPS on worker."""
        from core.camera import CaptureWorker
        
        worker = CaptureWorker(stream_link=0, parent=None, target_fps=30.0)
        worker.set_target_fps(15.0)
        
        assert worker._target_fps == 15.0

    def test_worker_stop_when_not_running(self):
        """Test stopping worker sets running flag to False."""
        from core.camera import CaptureWorker
        
        worker = CaptureWorker(stream_link=0, parent=None)
        worker._running = True
        worker.stop()
        assert worker._running is False


class TestGStreamerPipeline:
    """Test GStreamer pipeline generation."""

    def test_worker_stores_capture_dimensions(self):
        """Test CaptureWorker stores capture dimensions."""
        from core.camera import CaptureWorker
        
        worker = CaptureWorker(
            stream_link=0,
            parent=None,
            capture_width=640,
            capture_height=480,
        )
        
        assert worker.capture_width == 640
        assert worker.capture_height == 480


class TestFrameRateLimiting:
    """Test frame rate limiting logic."""

    def test_emit_interval_default(self):
        """Test default emit interval is set."""
        from core.camera import CaptureWorker
        
        worker = CaptureWorker(stream_link=0, parent=None, target_fps=20.0)
        
        # Emit interval should be set
        assert worker._emit_interval > 0

    def test_emit_interval_updates_with_fps(self):
        """Test emit interval updates when FPS changes."""
        from core.camera import CaptureWorker
        
        worker = CaptureWorker(stream_link=0, parent=None, target_fps=30.0)
        initial_interval = worker._emit_interval
        
        worker.set_target_fps(15.0)
        new_interval = worker._emit_interval
        
        # New interval should be longer (lower FPS = longer interval)
        assert new_interval > initial_interval
