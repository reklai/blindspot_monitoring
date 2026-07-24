"""Camera probing, stream-target, worker lifecycle, and frame-loop contracts.

Hardware and worker scheduling are replaced with deterministic doubles. The
tests retain real path and array behavior where those details are the contract,
so failures point to camera logic rather than the host's devices or timing.
"""

import time
from unittest.mock import MagicMock, patch, PropertyMock

import cv2
import pytest


@pytest.fixture(autouse=True)
def _isolate_by_path(tmp_path, monkeypatch):
    """Keep incidental discovery away from the host's ``/dev/v4l/by-path``.

    Several index-based entry points reach identity discovery transitively. A
    guaranteed-missing temporary directory forces their documented numeric
    fallback and resetting the one-shot warning prevents cross-test state.
    """
    import core.camera as camera_module

    monkeypatch.setattr(camera_module, "BY_PATH_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(camera_module, "_by_path_degraded_warned", False)


class TestGetVideoIndexes:
    """Numeric discovery remains available without USB port path identities."""

    def test_get_video_indexes_with_devices(self):
        """Discovery consistently exposes indexes as a list to index-based callers."""
        from core.camera import get_video_indexes
        
        # The host result is intentionally not asserted; this smoke case only pins
        # the return shape across machines with and without cameras.
        indexes = get_video_indexes()
        assert isinstance(indexes, list)

    def test_get_video_indexes_empty(self):
        """An empty device glob yields an empty candidate list."""
        with patch("core.camera.glob_module.glob") as mock_glob:
            mock_glob.return_value = []
            
            from core.camera import get_video_indexes
            indexes = get_video_indexes()
            assert indexes == []


class TestTestSingleCamera:
    """Define how one stream target is validated and normalized to an index."""

    def test_single_camera_success(self, mock_video_capture):
        """A capture that opens through the shared fixture returns its index."""
        from core.camera import test_single_camera
        
        result = test_single_camera(0, retries=1, retry_delay=0.01)
        assert result == 0

    def test_single_camera_failure(self):
        """Exhausting the open attempt reports an unusable camera as ``None``."""
        with patch("cv2.VideoCapture") as mock_cap:
            instance = MagicMock()
            instance.isOpened.return_value = False
            mock_cap.return_value = instance
            
            from core.camera import test_single_camera
            result = test_single_camera(99, retries=1, retry_delay=0.01)
            assert result is None

    def test_single_camera_retries(self):
        """Transient open failures are retried before the camera is rejected."""
        call_count = 0

        def mock_is_opened():
            nonlocal call_count
            call_count += 1
            # The third attempt models a device that settles shortly after discovery.
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
        """Integer targets open explicitly with V4L2 and return their index.

        String-target handling must not wrap or reinterpret numeric callers.
        """
        from core.camera import test_single_camera

        result = test_single_camera(5, retries=1, retry_delay=0.01)

        assert result == 5
        mock_video_capture.assert_called_once_with(5, cv2.CAP_V4L2)

    def test_single_camera_str_target_resolves_and_opens(self, tmp_path):
        """A by-path target opens its resolved node and still returns an index.

        Resolving first lets OpenCV and holder-recovery tools agree on the actual
        V4L2 node while preserving ``Optional[int]`` for callers.
        """
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
        """A stale by-path link fails before OpenCV receives a nonexistent node."""
        missing = tmp_path / "gone"

        with patch("cv2.VideoCapture") as mock_cap:
            from core.camera import test_single_camera
            result = test_single_camera(str(missing), retries=3, retry_delay=0.01)

        assert result is None
        mock_cap.assert_not_called()

    def test_single_camera_str_target_non_videon_returns_none(self, tmp_path):
        """Resolved strings outside the ``videoN`` convention are not V4L2 targets."""
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
        """Holder recovery receives the real node rather than its by-path alias.

        ``lsof`` and ``fuser`` report the opened node, so using the symlink would
        miss the process that must release the camera.
        """
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
        """Post-reclaim success preserves the same numeric return contract.

        This guards the less common retry branch from leaking its original path
        argument to callers that expect ``Optional[int]``.
        """
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
            # With one initial attempt, only the post-reclaim loop sees call two.
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
    """Pin the device forms accepted by the V4L2 GStreamer pipeline builder."""

    def test_int_device_matches_existing_pipeline_string(self):
        """An integer expands to its conventional ``/dev/videoN`` source."""
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
        """An explicit V4L2 path is inserted without altering the pipeline."""
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
        """Network streams fall outside this local-device pipeline helper."""
        from core.camera import _build_gstreamer_pipeline

        assert _build_gstreamer_pipeline("rtsp://example.com/stream", 640, 480) is None

    def test_non_dev_relative_string_returns_none(self):
        """Relative strings are rejected instead of producing an invalid source."""
        from core.camera import _build_gstreamer_pipeline

        assert _build_gstreamer_pipeline("some/relative/path", 640, 480) is None


class TestFindWorkingCameras:
    """``find_working_cameras`` exposes only validated camera indexes."""

    def test_find_working_cameras_returns_list(self, mock_video_capture):
        """Index discovery returns one list even when several candidates exist."""
        from core.camera import find_working_cameras
        
        with patch("core.camera.get_video_indexes", return_value=[0, 2, 4]):
            cameras = find_working_cameras()
            assert isinstance(cameras, list)

    def test_find_working_cameras_filters_invalid(self):
        """Candidates that fail validation do not reach index-based callers."""
        with patch("core.camera.get_video_indexes", return_value=[0, 1, 2]):
            with patch("core.camera.test_single_camera") as mock_test:
                # Keep one failure between two successes to exercise filtering
                # without making discovery order part of the expectation.
                mock_test.side_effect = lambda idx, **kw: idx if idx in [0, 2] else None
                
                from core.camera import find_working_cameras
                cameras = find_working_cameras()
                
                # Membership is the contract here; identity tests cover ordering.
                for cam in cameras:
                    assert cam in [0, 2]


class TestCaptureWorker:
    """Worker state follows the safety-sensitive thread stop sequence."""

    def test_worker_init(self):
        """Construction retains stream and capture parameters before the thread starts."""
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
        """Dynamic capture-rate updates replace the worker's active target."""
        from core.camera import CaptureWorker
        
        worker = CaptureWorker(stream_link=0, parent=None, target_fps=30.0)
        worker.set_target_fps(15.0)
        
        assert worker._target_fps == 15.0

    def test_worker_stop_when_not_running(self):
        """Stopping always closes the loop gate, including pre-start workers."""
        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link=0, parent=None)
        worker._running = True
        worker.stop()
        assert worker._running is False

    def test_stop_thread_exits_within_wait_closes_capture(self):
        """A normally exiting thread closes capture and reports clean disposal."""
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
        """A still-running thread keeps ownership of its capture.

        Releasing the OpenCV handle from the GUI thread can segfault while the
        worker is blocked inside it. The honest recovery path is to mark the
        worker leaked, retain the handle, and tell the operator to replug.
        """
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
        # The message must give the actionable recovery because no safe
        # in-process reclaim exists for this state.
        assert "leaking its fd" in caplog.text
        assert "replug" in caplog.text

    def test_stop_terminate_then_wait_succeeds_closes_capture(self):
        """Successful termination proceeds through the normal capture cleanup."""
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
        """Observed thread exit wins even if the second timed wait reports false."""
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
        """Numeric stream targets bypass filesystem resolution."""
        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link=4, parent=None)
        assert worker._resolve_stream_target() == 4

    def test_resolve_stream_target_no_caching_across_replug(self, tmp_path):
        """Each open resolves its by-path link after device re-enumeration.

        Udev may repoint the same by-path entry to a new ``videoN`` after a
        replug, so caching the first real path would reopen the stale node.
        """
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
        """Every V4L2 fallback attempt uses the resolved device node.

        The all-failing cascade also guards string targets from type assumptions
        that previously appeared only after the first backend failed.
        """
        import core.camera as camera_module
        from core.camera import CaptureWorker

        # Disable GStreamer so host-specific codec support cannot bypass the
        # V4L2 fallback sequence under test.
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
            # Reaching all fallback attempts with a string target is the regression case.
            worker._open_capture()

        assert mock_cap.call_args_list, "expected at least one open attempt"
        for call in mock_cap.call_args_list:
            assert call.args[0] == resolved


class TestGStreamerPipeline:
    """Workers retain the dimensions consumed by GStreamer construction."""

    def test_worker_stores_capture_dimensions(self):
        """Requested dimensions remain available when a backend opens later."""
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
    """Capture-rate updates keep emission-interval bookkeeping coherent."""

    def test_emit_interval_default(self):
        """A positive target produces a usable throttle interval at construction."""
        from core.camera import CaptureWorker
        
        worker = CaptureWorker(stream_link=0, parent=None, target_fps=20.0)
        
        assert worker._emit_interval > 0

    def test_emit_interval_updates_with_fps(self):
        """Lowering FPS lengthens the delay between emitted frames."""
        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link=0, parent=None, target_fps=30.0)
        initial_interval = worker._emit_interval

        worker.set_target_fps(15.0)
        new_interval = worker._emit_interval

        # Compare direction rather than implementation arithmetic.
        assert new_interval > initial_interval


class TestEmitRateAlignment:
    """Keep worker emissions at ``min(capture_fps, ui_fps)``.

    Emitting faster than the UI can render wastes decode and signal traffic, so
    both initial configuration and later rate changes maintain this invariant.
    """

    def test_emit_interval_bounded_by_ui_fps(self):
        """When capture is faster, the UI rate sets the emission interval."""
        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link=0, target_fps=25.0, ui_fps=20.0)
        worker._configure_fps_from_camera()
        assert worker._emit_interval == pytest.approx(1.0 / 20.0)

    def test_emit_interval_uses_capture_when_below_ui(self):
        """When capture is slower, no artificial UI-rate speedup is attempted."""
        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link=0, target_fps=10.0, ui_fps=20.0)
        worker._configure_fps_from_camera()
        assert worker._emit_interval == pytest.approx(1.0 / 10.0)

    def test_emit_interval_unbounded_without_ui_fps(self):
        """Without a UI bound, emission follows the configured capture rate."""
        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link=0, target_fps=25.0)
        worker._configure_fps_from_camera()
        assert worker._emit_interval == pytest.approx(1.0 / 25.0)

    def test_set_ui_fps_lowers_emit_rate_to_new_bound(self):
        """A runtime UI slowdown immediately tightens the worker's bound."""
        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link=0, target_fps=25.0, ui_fps=20.0)
        worker._configure_fps_from_camera()
        assert worker._emit_interval == pytest.approx(1.0 / 20.0)

        worker.set_ui_fps(12.0)
        assert worker._emit_interval == pytest.approx(1.0 / 12.0)

    def test_set_target_fps_stays_bounded_by_ui(self):
        """Capture-rate changes cannot raise emission above the current UI rate."""
        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link=0, target_fps=25.0, ui_fps=20.0)
        worker._configure_fps_from_camera()

        # Below the UI bound, capture is the limiting side of the invariant.
        worker.set_target_fps(10.0)
        assert worker._emit_interval == pytest.approx(1.0 / 10.0)

        # Crossing back above the bound must not restore the faster emit rate.
        worker.set_target_fps(25.0)
        assert worker._emit_interval == pytest.approx(1.0 / 20.0)


def _run_worker_for_grabs(worker, cap, n_grabs):
    """Run the capture loop deterministically for a fixed number of grabs.

    The helper injects an already-open mock capture, removes sleeping, and
    lowers ``_running`` from ``grab``'s side effect. Tests can then inspect
    decode calls and emitted objects without starting a ``QThread``.
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
    """Protect the capture loop's decode avoidance and zero-copy handoff."""

    def test_emit_sends_retrieve_array_identity_no_copy(self):
        """An admitted frame is the exact array returned by ``retrieve``."""
        import numpy as np

        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link=0, parent=None, target_fps=30.0)
        # A zero interval isolates handoff behavior from timing.
        worker._emit_interval = 0.0
        worker._last_emit = 0.0

        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        cap = MagicMock()
        cap.retrieve.return_value = (True, frame)

        emitted = []
        worker.frame_ready.connect(lambda f: emitted.append(f))

        _run_worker_for_grabs(worker, cap, n_grabs=1)

        assert len(emitted) == 1
        # Object identity is the regression guard against a hidden buffer copy.
        assert emitted[0] is frame

    def test_throttle_before_retrieve_skips_decode_for_dropped_frames(self):
        """Throttled frames are grabbed for freshness but not decoded.

        On the V4L2 MJPG path, ``retrieve`` performs the expensive JPEG decode.
        A very large interval admits only the first of four grabs, pinning the
        optimization that places the throttle before retrieval.
        """
        import numpy as np

        from core.camera import CaptureWorker

        worker = CaptureWorker(stream_link=0, parent=None, target_fps=30.0)
        # This interval is deliberately far longer than the synchronous test run.
        worker._emit_interval = 10_000.0
        worker._last_emit = 0.0

        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        cap = MagicMock()
        cap.retrieve.return_value = (True, frame)

        emitted = []
        worker.frame_ready.connect(lambda f: emitted.append(f))

        _run_worker_for_grabs(worker, cap, n_grabs=4)

        assert cap.grab.call_count == 4
        assert cap.retrieve.call_count == 1
        assert len(emitted) == 1


class TestEmitThrottle:
    """Software emission throttling preserves phase across skipped frames.

    The capture loop sees one decision per grabbed frame. When the capture and
    UI rates are not exact divisors, discarding the credit past each deadline
    quantizes the emit rate down an entire frame period, so these tests pin the
    long-run average rather than individual frame choices.
    """

    def _make_worker(self, target_fps: float, ui_fps: float):
        from core.camera import CaptureWorker

        return CaptureWorker(
            stream_link=0, parent=None, target_fps=target_fps, ui_fps=ui_fps
        )

    def test_non_divisor_rates_average_the_ui_bound(self):
        """25 FPS frames against a 20 FPS bound emit ~20 FPS, not every other frame."""
        worker = self._make_worker(target_fps=25.0, ui_fps=20.0)
        assert worker._emit_interval == pytest.approx(0.05)

        base = 1_000_000.0
        emitted = sum(worker._emit_due(base + i * 0.04) for i in range(50))

        # 2 simulated seconds at 20 FPS; phase-resetting logic yields only 25.
        assert 38 <= emitted <= 42

    def test_frames_slower_than_interval_emit_every_frame(self):
        """Frames arriving well past each deadline are never throttled."""
        worker = self._make_worker(target_fps=25.0, ui_fps=20.0)

        base = 1_000_000.0
        emitted = sum(worker._emit_due(base + i * 0.1) for i in range(20))

        assert emitted == 20

    def test_stall_does_not_bank_catchup_emissions(self):
        """A long offline gap restarts the cadence instead of bursting.

        Advancing a deadline by fixed intervals alone would owe hundreds of
        emissions after a stall and pass every frame through until repaid.
        """
        worker = self._make_worker(target_fps=25.0, ui_fps=20.0)

        base = 1_000_000.0
        for i in range(5):
            worker._emit_due(base + i * 0.04)

        resume = base + 10.0
        assert worker._emit_due(resume) is True
        # The very next frame is inside the restarted interval.
        assert worker._emit_due(resume + 0.04) is False
        emitted = 1 + sum(
            worker._emit_due(resume + 0.04 * i) for i in range(2, 26)
        )

        # 1 simulated second after resume stays at the 20 FPS average; an
        # unclamped deadline would emit all 25 frames.
        assert emitted <= 22
