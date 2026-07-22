"""
Tests for core/camera.py - Camera discovery and capture logic.
"""

import time
from unittest.mock import MagicMock, patch, PropertyMock

import cv2
import pytest


@pytest.fixture(autouse=True)
def _isolate_by_path(tmp_path, monkeypatch):
    """Point BY_PATH_DIR at a nonexistent tmp path for every test in this
    module, so tests that (transitively, via find_working_cameras) touch
    /dev/v4l/by-path stay environment-independent instead of reading
    whatever real by-path tree happens to exist on the machine running
    them."""
    import core.camera as camera_module

    monkeypatch.setattr(camera_module, "BY_PATH_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(camera_module, "_by_path_degraded_warned", False)


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

    def test_single_camera_int_target_call_form_unchanged(self, mock_video_capture):
        """Regression guard: an int target must call cv2.VideoCapture with
        the exact same args as before device-path support (no CAP_V4L2
        change, no re-wrapping of the index)."""
        from core.camera import test_single_camera

        result = test_single_camera(5, retries=1, retry_delay=0.01)

        assert result == 5
        mock_video_capture.assert_called_once_with(5, cv2.CAP_V4L2)

    def test_single_camera_str_target_resolves_and_opens(self, tmp_path):
        """str target: realpath'd first; cv2.VideoCapture is called with
        the RESOLVED path (not the symlink); success returns the numeric
        index parsed from the resolved /dev/videoN node."""
        video_node = tmp_path / "video7"
        video_node.write_text("")
        symlink = tmp_path / "by-path-camera"
        symlink.symlink_to(video_node)
        resolved = str(video_node.resolve())

        with patch("cv2.VideoCapture") as mock_cap:
            instance = MagicMock()
            instance.isOpened.return_value = True
            instance.grab.return_value = True
            mock_cap.return_value = instance

            from core.camera import test_single_camera
            result = test_single_camera(str(symlink), retries=1, retry_delay=0.01)

        assert result == 7
        mock_cap.assert_called_once_with(resolved, cv2.CAP_V4L2)

    def test_single_camera_str_target_missing_realpath_returns_none(self, tmp_path):
        """str target whose realpath doesn't exist fails fast: returns
        None without ever calling cv2.VideoCapture."""
        missing = tmp_path / "gone"

        with patch("cv2.VideoCapture") as mock_cap:
            from core.camera import test_single_camera
            result = test_single_camera(str(missing), retries=3, retry_delay=0.01)

        assert result is None
        mock_cap.assert_not_called()

    def test_single_camera_str_target_non_videon_returns_none(self, tmp_path):
        """str target resolving to a node that isn't a /dev/videoN path
        returns None (V4L2-only support)."""
        node = tmp_path / "not-a-video-node"
        node.write_text("")

        with patch("cv2.VideoCapture") as mock_cap:
            from core.camera import test_single_camera
            result = test_single_camera(str(node), retries=1, retry_delay=0.01)

        assert result is None
        mock_cap.assert_not_called()

    def test_single_camera_str_target_kill_uses_resolved_path(
        self, tmp_path, save_restore_config
    ):
        """When allow_kill triggers, kill_device_holders receives the
        RESOLVED real path, never the symlink (lsof/fuser can't match a
        symlink)."""
        from core import config

        video_node = tmp_path / "video2"
        video_node.write_text("")
        symlink = tmp_path / "by-path-camera"
        symlink.symlink_to(video_node)
        resolved = str(video_node.resolve())

        config.KILL_DEVICE_HOLDERS = True

        with patch("cv2.VideoCapture") as mock_cap:
            instance = MagicMock()
            instance.isOpened.return_value = False
            mock_cap.return_value = instance

            with patch("core.camera.kill_device_holders") as mock_kill:
                mock_kill.return_value = False

                from core.camera import test_single_camera
                result = test_single_camera(
                    str(symlink),
                    retries=1,
                    retry_delay=0.01,
                    allow_kill=True,
                    post_kill_retries=1,
                    post_kill_delay=0.01,
                )

                mock_kill.assert_called_once_with(resolved)

        assert result is None

    def test_single_camera_str_target_post_kill_success_returns_numeric_index(
        self, tmp_path, save_restore_config
    ):
        """str target that fails the initial retries but succeeds after
        kill_device_holders must return the parsed NUMERIC index (an
        int), never the original path string -- honoring the
        -> Optional[int] contract regardless of which retry loop
        succeeded."""
        from core import config

        video_node = tmp_path / "video9"
        video_node.write_text("")
        symlink = tmp_path / "by-path-camera"
        symlink.symlink_to(video_node)

        config.KILL_DEVICE_HOLDERS = True

        call_count = 0

        def mock_is_opened():
            nonlocal call_count
            call_count += 1
            # Fail every initial-retry attempt; succeed only once kill
            # has been attempted (i.e. on the post-kill retry loop).
            return call_count > 1

        with patch("cv2.VideoCapture") as mock_cap:
            instance = MagicMock()
            instance.isOpened.side_effect = mock_is_opened
            instance.grab.return_value = True
            mock_cap.return_value = instance

            with patch("core.camera.kill_device_holders", return_value=True):
                from core.camera import test_single_camera
                result = test_single_camera(
                    str(symlink),
                    retries=1,
                    retry_delay=0.01,
                    allow_kill=True,
                    post_kill_retries=2,
                    post_kill_delay=0.01,
                )

        assert result == 9
        assert isinstance(result, int)


class TestBuildGstreamerPipeline:
    """Test the extracted GStreamer pipeline string builder."""

    def test_int_device_matches_existing_pipeline_string(self):
        from core.camera import _build_gstreamer_pipeline

        pipeline = _build_gstreamer_pipeline(3, 640, 480)

        assert pipeline == (
            "v4l2src device=/dev/video3 ! "
            "image/jpeg,width=640,height=480 ! "
            "queue max-size-buffers=2 leaky=downstream ! "
            "jpegdec ! videoconvert ! "
            "appsink drop=1 max-buffers=1 sync=false"
        )

    def test_dev_path_string_uses_device_form(self):
        from core.camera import _build_gstreamer_pipeline

        pipeline = _build_gstreamer_pipeline("/dev/video5", 640, 480)

        assert pipeline == (
            "v4l2src device=/dev/video5 ! "
            "image/jpeg,width=640,height=480 ! "
            "queue max-size-buffers=2 leaky=downstream ! "
            "jpegdec ! videoconvert ! "
            "appsink drop=1 max-buffers=1 sync=false"
        )

    def test_rtsp_url_returns_none(self):
        from core.camera import _build_gstreamer_pipeline

        assert _build_gstreamer_pipeline("rtsp://example.com/stream", 640, 480) is None

    def test_non_dev_relative_string_returns_none(self):
        from core.camera import _build_gstreamer_pipeline

        assert _build_gstreamer_pipeline("some/relative/path", 640, 480) is None


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

    def test_stop_thread_exits_within_wait_closes_capture(self):
        """wait(2000) succeeds -> thread confirmed dead, capture closed from
        stop() (belt-and-braces), returns True, not leaked."""
        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link=0, parent=None)
        worker.wait = MagicMock(return_value=True)
        worker.terminate = MagicMock()
        worker._close_capture = MagicMock()

        result = worker.stop()

        assert result is True
        worker._close_capture.assert_called_once()
        worker.terminate.assert_not_called()
        assert worker.is_leaked is False

    def test_stop_unkillable_thread_leaks_capture(self, caplog):
        """Both waits fail and isRunning stays True -> DO NOT release capture
        from the main thread (segfault risk); returns False, leaked flag set,
        ERROR logged."""
        import logging

        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link="/dev/video0", parent=None)
        worker.wait = MagicMock(return_value=False)
        worker.isRunning = MagicMock(return_value=True)
        worker.terminate = MagicMock()
        worker._close_capture = MagicMock()

        with caplog.at_level(logging.ERROR):
            result = worker.stop()

        assert result is False
        worker._close_capture.assert_not_called()
        worker.terminate.assert_called_once()
        assert worker.is_leaked is True
        assert "leaking capture handle" in caplog.text

    def test_stop_terminate_then_wait_succeeds_closes_capture(self):
        """First wait fails, terminate() called, second wait succeeds ->
        thread gone, capture closed from stop(), returns True, not leaked."""
        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link=0, parent=None)
        worker.wait = MagicMock(side_effect=[False, True])
        worker.isRunning = MagicMock(return_value=True)
        worker.terminate = MagicMock()
        worker._close_capture = MagicMock()

        result = worker.stop()

        assert result is True
        worker.terminate.assert_called_once()
        worker._close_capture.assert_called_once()
        assert worker.is_leaked is False

    def test_stop_terminate_then_not_running_closes_capture(self):
        """Second wait times out but isRunning() is False -> thread gone
        anyway, capture closed, returns True."""
        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link=0, parent=None)
        worker.wait = MagicMock(side_effect=[False, False])
        worker.isRunning = MagicMock(return_value=False)
        worker.terminate = MagicMock()
        worker._close_capture = MagicMock()

        result = worker.stop()

        assert result is True
        worker._close_capture.assert_called_once()
        assert worker.is_leaked is False

    def test_resolve_stream_target_int_passthrough(self):
        """int stream_link resolves to itself, unchanged."""
        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link=4, parent=None)
        assert worker._resolve_stream_target() == 4

    def test_resolve_stream_target_no_caching_across_replug(self, tmp_path):
        """str stream_link is realpath'd on EVERY call -- no caching. A
        by-path symlink re-pointed between two calls (simulating udev
        re-pointing it after a replug) must resolve to the NEW target on
        the second call."""
        from core.camera import CaptureWorker

        node_a = tmp_path / "video0"
        node_a.write_text("")
        node_b = tmp_path / "video1"
        node_b.write_text("")
        symlink = tmp_path / "by-path-camera"
        symlink.symlink_to(node_a)

        worker = CaptureWorker(stream_link=str(symlink), parent=None)
        first = worker._resolve_stream_target()
        assert first == str(node_a.resolve())

        symlink.unlink()
        symlink.symlink_to(node_b)

        second = worker._resolve_stream_target()
        assert second == str(node_b.resolve())
        assert second != first

    def test_worker_str_stream_link_open_uses_resolved_path(self, tmp_path, monkeypatch):
        """Constructing a worker with a str device path and running
        _open_capture through its (mocked, failing) fallback cascade must
        not crash, and every cv2.VideoCapture call must use the RESOLVED
        path, never the symlink."""
        import core.camera as camera_module
        from core.camera import CaptureWorker

        # Keep this test to the V4L2 cascade regardless of the local
        # OpenCV build's GStreamer support.
        monkeypatch.setattr(camera_module.config, "USE_GSTREAMER", False)

        video_node = tmp_path / "video4"
        video_node.write_text("")
        symlink = tmp_path / "by-path-camera"
        symlink.symlink_to(video_node)
        resolved = str(video_node.resolve())

        with patch("cv2.VideoCapture") as mock_cap:
            instance = MagicMock()
            instance.isOpened.return_value = False
            mock_cap.return_value = instance

            worker = CaptureWorker(stream_link=str(symlink), parent=None)
            worker._open_capture()  # must not raise

        assert mock_cap.call_args_list, "expected at least one open attempt"
        for call in mock_cap.call_args_list:
            assert call.args[0] == resolved


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


def _run_worker_for_grabs(worker, cap, n_grabs):
    """Drive worker.run() synchronously for exactly `n_grabs` grab() calls.

    Sets worker._cap to the mock capture, makes msleep a no-op, and stops
    the loop after `n_grabs` grab() calls. Returns nothing; inspect the
    mock and any connected collectors afterward.
    """
    cap.isOpened.return_value = True
    worker._cap = cap
    worker.msleep = lambda ms: None

    state = {"grabs": 0}

    def grab_side_effect():
        state["grabs"] += 1
        if state["grabs"] >= n_grabs:
            worker._running = False
        return True

    cap.grab.side_effect = grab_side_effect
    worker.run()


class TestRunLoopEmit:
    """Behavioral tests for the run() capture loop: throttle-before-retrieve
    and direct (no-copy) emission of the retrieve() array."""

    def test_emit_sends_retrieve_array_identity_no_copy(self):
        """With the throttle open every iteration, the emitted object IS the
        exact numpy array retrieve() returned -- no pool copy in between."""
        import numpy as np

        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link=0, parent=None, target_fps=30.0)
        # Open the throttle fully so every grabbed frame emits.
        worker._emit_interval = 0.0
        worker._last_emit = 0.0

        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        cap = MagicMock()
        cap.retrieve.return_value = (True, frame)

        emitted = []
        worker.frame_ready.connect(lambda f: emitted.append(f))

        _run_worker_for_grabs(worker, cap, n_grabs=1)

        assert len(emitted) == 1
        # Identity, not just equality: no copy was made.
        assert emitted[0] is frame
