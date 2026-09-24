"""
Tests for core/camera.py - Camera discovery and the capture loop.
"""

import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core import camera
from core.camera import CaptureWorker, find_working_cameras, get_video_indexes

# Referenced through the module so pytest does not collect it as a test.
probe_camera = camera.test_single_camera


class TestGetVideoIndexes:
    def test_empty(self):
        with patch("core.camera.glob_module.glob", return_value=[]):
            assert get_video_indexes() == []

    def test_numeric_sort_and_skips_non_numeric(self):
        devices = ["/dev/video10", "/dev/video2", "/dev/video0", "/dev/video-extra"]
        with patch("core.camera.glob_module.glob", return_value=devices):
            assert get_video_indexes() == [0, 2, 10]


class TestTestSingleCamera:
    def test_success(self, mock_video_capture):
        assert probe_camera(0, retries=1, retry_delay=0.01) == 0

    def test_failure_returns_none(self):
        with patch("cv2.VideoCapture") as mock_cap:
            instance = MagicMock()
            instance.isOpened.return_value = False
            mock_cap.return_value = instance
            assert probe_camera(99, retries=1, retry_delay=0.01) is None

    def test_retries_until_open(self):
        calls = {"n": 0}

        def is_opened():
            calls["n"] += 1
            return calls["n"] >= 3

        with patch("cv2.VideoCapture") as mock_cap:
            instance = MagicMock()
            instance.isOpened.side_effect = is_opened
            mock_cap.return_value = instance
            assert probe_camera(0, retries=3, retry_delay=0.01) == 0
            assert calls["n"] >= 2

    def test_kills_holders_then_retries(self, save_restore_config):
        from core import config
        config.KILL_DEVICE_HOLDERS = True
        attempts = {"n": 0}

        def is_opened():
            attempts["n"] += 1
            return attempts["n"] > 2  # fails during the first pass, succeeds after the kill

        with patch("cv2.VideoCapture") as mock_cap, \
             patch("core.camera.kill_device_holders", return_value=True) as mock_kill:
            instance = MagicMock()
            instance.isOpened.side_effect = is_opened
            mock_cap.return_value = instance
            assert probe_camera(4, retries=2, retry_delay=0.0, post_kill_delay=0.0) == 4
            mock_kill.assert_called_once_with("/dev/video4")

    def test_no_kill_when_disallowed(self, save_restore_config):
        from core import config
        config.KILL_DEVICE_HOLDERS = True
        with patch("cv2.VideoCapture") as mock_cap, \
             patch("core.camera.kill_device_holders") as mock_kill:
            instance = MagicMock()
            instance.isOpened.return_value = False
            mock_cap.return_value = instance
            assert probe_camera(1, retries=1, retry_delay=0.0, allow_kill=False) is None
            mock_kill.assert_not_called()


class TestFindWorkingCameras:
    def test_filters_invalid_and_sorts(self):
        with patch("core.camera.get_video_indexes", return_value=[2, 0, 1]), \
             patch("core.camera.test_single_camera") as mock_test:
            mock_test.side_effect = lambda idx, **kw: idx if idx in (0, 2) else None
            assert find_working_cameras() == [0, 2]

    def test_second_pass_confirms_without_kill(self):
        seen = []

        def probe(idx, **kw):
            seen.append((idx, kw.get("allow_kill", True)))
            return idx

        with patch("core.camera.get_video_indexes", return_value=[0]), \
             patch("core.camera.test_single_camera", side_effect=probe):
            assert find_working_cameras() == [0]
        assert seen == [(0, True), (0, False)]

    def test_no_devices(self):
        with patch("core.camera.get_video_indexes", return_value=[]):
            assert find_working_cameras() == []


class TestCaptureWorkerApi:
    def test_no_target_fps_uses_camera_default_until_open(self):
        w = CaptureWorker(0)
        assert w.target_fps is None
        assert w.emit_interval == pytest.approx(1 / CaptureWorker.DEFAULT_CAMERA_FPS)

    def test_set_target_fps_updates_interval(self):
        w = CaptureWorker(0, target_fps=30.0)
        before = w.emit_interval
        w.set_target_fps(15.0)
        assert w.target_fps == 15.0
        assert w.emit_interval > before

    def test_set_target_fps_ignores_invalid(self):
        w = CaptureWorker(0, target_fps=30.0)
        for bad in (None, 0, -5, "fast"):
            w.set_target_fps(bad)
        assert w.target_fps == 30.0

    def test_stop_before_start_is_safe_and_idempotent(self):
        w = CaptureWorker(0)
        w.stop()
        w.stop()
        assert w.stop_requested
        assert not w.isRunning()

class _FakeCapture:
    """Stand-in for cv2.VideoCapture that delivers frames on a fixed clock."""

    def __init__(self, frame_period: float):
        self.frame_period = frame_period
        self.grabs = 0
        self.retrieves = 0
        self._t = 0.0

    def isOpened(self):
        return True

    def grab(self):
        self.grabs += 1
        self._t += self.frame_period
        return True

    def retrieve(self):
        self.retrieves += 1
        return True, np.zeros((4, 4, 3), np.uint8)

    def release(self):
        pass


class TestCaptureLoop:
    """Drive CaptureWorker._step directly with a fake device and a fake clock."""

    def _worker_with_fake_cap(self, target_fps, source_fps):
        w = CaptureWorker(0, target_fps=target_fps)
        fake = _FakeCapture(1.0 / source_fps)
        w._cap = fake
        w._online = True
        w.msleep = lambda ms: None
        return w, fake

    def test_retrieve_only_for_emitted_frames(self):
        # Source at 40 FPS, target 10 FPS: every frame is grabbed (to drain the
        # driver queue) but only one in four is decoded and emitted.
        w, fake = self._worker_with_fake_cap(target_fps=10, source_fps=40)
        emitted = []
        w.frame_ready.connect(emitted.append)
        clock = {"t": 0.0}

        def fake_time():
            clock["t"] += 1 / 40
            return clock["t"]

        with patch("core.camera.time.time", side_effect=fake_time):
            for _ in range(40):
                w._step()
        assert fake.grabs == 40
        assert fake.retrieves == len(emitted)
        assert 10 <= fake.retrieves <= 11

    def test_grab_failure_closes_and_goes_offline(self):
        w, fake = self._worker_with_fake_cap(target_fps=10, source_fps=10)
        fake.grab = lambda: False
        statuses = []
        w.status_changed.connect(statuses.append)
        w._step()
        assert w._cap is None
        assert statuses == [False]

    def test_open_failure_backs_off(self):
        w = CaptureWorker(0, target_fps=10)
        w._open_capture = lambda: False
        w._stop_event.wait = MagicMock()
        w._step()
        w._stop_event.wait.assert_called_once_with(CaptureWorker.RECONNECT_MIN_SEC)
        assert w._reconnect_backoff == pytest.approx(CaptureWorker.RECONNECT_MIN_SEC * 1.5)
        w._step()
        assert w._reconnect_backoff == pytest.approx(CaptureWorker.RECONNECT_MIN_SEC * 1.5 ** 2)

    def test_successful_open_resets_backoff_and_reports_online(self):
        w = CaptureWorker(0, target_fps=10)
        w._reconnect_backoff = 7.0
        fake = _FakeCapture(0.1)

        def open_ok():
            w._cap = fake
            return True

        w._open_capture = open_ok
        w.msleep = lambda ms: None
        statuses = []
        w.status_changed.connect(statuses.append)
        w._step()
        assert statuses == [True]
        assert w._reconnect_backoff == CaptureWorker.RECONNECT_MIN_SEC
        assert fake.grabs == 1

    def test_is_healthy_reflects_recent_emit(self):
        w = CaptureWorker(0)
        assert not w.is_healthy()  # not running
        with patch.object(CaptureWorker, "isRunning", return_value=True):
            w._start_ts = time.time()
            assert w.is_healthy()
            w._last_emit = time.time() - CaptureWorker.HEALTHY_SILENCE_SEC - 1
            assert not w.is_healthy()
