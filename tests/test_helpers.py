"""
Tests for utils/helpers.py utility functions.
"""

import os
import signal
import subprocess
from unittest import mock

import pytest

from utils import helpers


class TestRunCmd:
    """Tests for run_cmd function."""

    def test_run_cmd_success(self):
        """Test successful command execution."""
        stdout, stderr, code = helpers.run_cmd("echo hello")
        assert code == 0
        assert stdout == "hello"
        assert stderr == ""

    def test_run_cmd_failure(self):
        """Test command that fails."""
        stdout, stderr, code = helpers.run_cmd("false")
        assert code == 1

    def test_run_cmd_timeout(self):
        """Test command timeout returns error."""
        stdout, stderr, code = helpers.run_cmd("sleep 10", timeout=1)
        assert code == 1
        assert stdout == ""

    def test_run_cmd_invalid_command(self):
        """Test invalid command returns error."""
        stdout, stderr, code = helpers.run_cmd("nonexistent_command_xyz")
        assert code != 0 or stderr != ""


class TestGetPidsFromLsof:
    """Tests for get_pids_from_lsof function."""

    def test_get_pids_empty_when_no_device(self):
        """Test returns empty set for non-existent device."""
        pids = helpers.get_pids_from_lsof("/dev/nonexistent_device_xyz")
        assert pids == set()

    @mock.patch("utils.helpers.run_cmd")
    def test_get_pids_parses_output(self, mock_run):
        """Test parsing of lsof output."""
        mock_run.return_value = ("1234\n5678\n", "", 0)
        pids = helpers.get_pids_from_lsof("/dev/video0")
        assert pids == {1234, 5678}

    @mock.patch("utils.helpers.run_cmd")
    def test_get_pids_handles_non_numeric(self, mock_run):
        """Test graceful handling of non-numeric output."""
        mock_run.return_value = ("1234\nabc\n5678\n", "", 0)
        pids = helpers.get_pids_from_lsof("/dev/video0")
        assert pids == {1234, 5678}

    @mock.patch("utils.helpers.run_cmd")
    def test_get_pids_returns_empty_on_failure(self, mock_run):
        """Test returns empty set on command failure."""
        mock_run.return_value = ("", "error", 1)
        pids = helpers.get_pids_from_lsof("/dev/video0")
        assert pids == set()


class TestGetPidsFromFuser:
    """Tests for get_pids_from_fuser function."""

    @mock.patch("utils.helpers.run_cmd")
    def test_get_pids_parses_fuser_output(self, mock_run):
        """Test parsing of fuser output with regex."""
        # fuser output format varies - it outputs to stderr and may have suffixes like 'm'
        # The regex looks for digit sequences, so "5678m" would only match 5678
        mock_run.return_value = ("/dev/video0: 1234 5678", "", 0)
        pids = helpers.get_pids_from_fuser("/dev/video0")
        assert 1234 in pids
        assert 5678 in pids

    @mock.patch("utils.helpers.run_cmd")
    def test_get_pids_returns_empty_on_failure(self, mock_run):
        """Test returns empty set on command failure."""
        mock_run.return_value = ("", "", 1)
        pids = helpers.get_pids_from_fuser("/dev/video0")
        assert pids == set()


class TestIsPidAlive:
    """Tests for is_pid_alive function."""

    def test_current_process_is_alive(self):
        """Test that current process is detected as alive."""
        assert helpers.is_pid_alive(os.getpid()) is True

    def test_nonexistent_pid_not_alive(self):
        """Test that very high PID is not alive."""
        # Use a PID that almost certainly doesn't exist
        assert helpers.is_pid_alive(999999999) is False


class TestKillDeviceHolders:
    """Tests for kill_device_holders function."""

    @mock.patch("utils.helpers.get_pids_from_lsof")
    @mock.patch("core.config.KILL_DEVICE_HOLDERS", False)
    def test_disabled_when_config_false(self, mock_lsof):
        """Test function does nothing when config disabled."""
        result = helpers.kill_device_holders("/dev/video0")
        assert result is False
        mock_lsof.assert_not_called()

    @mock.patch("utils.helpers.get_pids_from_lsof")
    @mock.patch("utils.helpers.get_pids_from_fuser")
    @mock.patch("core.config.KILL_DEVICE_HOLDERS", True)
    def test_returns_false_when_no_holders(self, mock_fuser, mock_lsof):
        """Test returns False when no processes hold device."""
        mock_lsof.return_value = set()
        mock_fuser.return_value = set()
        result = helpers.kill_device_holders("/dev/video0")
        assert result is False

    @mock.patch("utils.helpers.is_pid_alive")
    @mock.patch("utils.helpers.get_pids_from_lsof")
    @mock.patch("utils.helpers.get_pids_from_fuser")
    @mock.patch("os.kill")
    @mock.patch("time.sleep")
    @mock.patch("core.config.KILL_DEVICE_HOLDERS", True)
    def test_kills_processes_with_sigterm(
        self, mock_sleep, mock_kill, mock_fuser, mock_lsof, mock_alive
    ):
        """Test sends SIGTERM to holding processes."""
        fake_pid = 12345
        mock_lsof.return_value = {fake_pid}
        mock_fuser.return_value = set()
        mock_alive.return_value = False  # Process dies after SIGTERM

        result = helpers.kill_device_holders("/dev/video0", grace=0.1)

        assert result is True
        mock_kill.assert_any_call(fake_pid, signal.SIGTERM)


class TestLogHealthSummary:
    """Tests for log_health_summary function."""

    @mock.patch("logging.info")
    @mock.patch("logging.warning")
    def test_logs_health_summary(self, mock_warning, mock_log):
        """Test logs camera health information."""
        import time
        now = time.time()
        
        # Create mock camera widgets with required attributes
        mock_widget1 = mock.MagicMock()
        mock_widget1._latest_frame = "frame_data"
        mock_widget1._last_frame_ts = now  # fresh frame
        mock_widget1.worker = None
        mock_widget1.camera_stream_link = 0
        
        mock_widget2 = mock.MagicMock()
        mock_widget2._latest_frame = None
        mock_widget2._last_frame_ts = 0.0
        mock_widget2.worker = None
        mock_widget2.camera_stream_link = 2
        
        mock_widget3 = mock.MagicMock()
        mock_widget3._latest_frame = "frame_data"
        mock_widget3._last_frame_ts = now  # fresh frame
        mock_widget3.worker = None
        mock_widget3.camera_stream_link = 4

        camera_widgets = [mock_widget1, mock_widget2, mock_widget3]
        placeholder_slots = [mock.MagicMock()]
        active_indexes = {0, 2, 4}
        failed_indexes = {6: 123.0}

        helpers.log_health_summary(
            camera_widgets, placeholder_slots, active_indexes, failed_indexes
        )

        mock_log.assert_called_once()
        call_args = mock_log.call_args
        assert "Health" in call_args[0][0]
        assert call_args[0][1] == 2  # online count (widgets with fresh frames)
    
    @mock.patch("logging.info")
    @mock.patch("logging.warning")
    def test_detects_stale_frames(self, mock_warning, mock_log):
        """Test that stale frames are detected and logged."""
        import time
        now = time.time()
        
        # Create widget with stale frame (last frame 15 seconds ago)
        mock_widget = mock.MagicMock()
        mock_widget._latest_frame = "frame_data"
        mock_widget._last_frame_ts = now - 15.0  # stale
        mock_widget.worker = None
        mock_widget.camera_stream_link = 0

        helpers.log_health_summary(
            [mock_widget], [], set(), {}
        )
        
        # Should log a warning about stale frame
        mock_warning.assert_called()
        warning_call = mock_warning.call_args[0][0]
        assert "stale" in warning_call.lower()
    
    @mock.patch("logging.info")
    @mock.patch("logging.warning")
    def test_detects_unhealthy_worker(self, mock_warning, mock_log):
        """Test that unhealthy workers are detected and logged."""
        import time
        now = time.time()
        
        # Create widget with unhealthy worker
        mock_worker = mock.MagicMock()
        mock_worker.is_healthy.return_value = False
        
        mock_widget = mock.MagicMock()
        mock_widget._latest_frame = "frame_data"
        mock_widget._last_frame_ts = now
        mock_widget.worker = mock_worker
        mock_widget.camera_stream_link = 0

        helpers.log_health_summary(
            [mock_widget], [], set(), {}
        )
        
        # Should log a warning about unhealthy worker
        mock_warning.assert_called()
        warning_call = mock_warning.call_args[0][0]
        assert "unhealthy" in warning_call.lower()


class TestSetCloexecOnDeviceFds:
    """set_cloexec_on_device_fds marks open fds whose /proc/self/fd target
    matches the prefix as close-on-exec, so the settings-tile restart's
    os.execv does not carry a leaked capture fd into the replacement
    process. OpenCV's V4L2 open() has no O_CLOEXEC, and exec keeps the PID,
    so kill_device_holders (which skips our own PID) could never reclaim
    the device in the restarted app -- the kernel must drop the fd at exec.
    """

    def test_sets_cloexec_only_on_matching_fds(self, tmp_path):
        device = tmp_path / "video7"
        device.write_bytes(b"")
        other = tmp_path / "unrelated"
        other.write_bytes(b"")

        dev_fd = os.open(device, os.O_RDONLY)
        other_fd = os.open(other, os.O_RDONLY)
        try:
            # Simulate OpenCV's V4L2 open(): fd inheritable (no CLOEXEC).
            os.set_inheritable(dev_fd, True)
            os.set_inheritable(other_fd, True)

            count = helpers.set_cloexec_on_device_fds(
                prefix=str(tmp_path / "video")
            )

            assert count == 1
            # inheritable is the inverse of FD_CLOEXEC.
            assert os.get_inheritable(dev_fd) is False
            assert os.get_inheritable(other_fd) is True
        finally:
            os.close(dev_fd)
            os.close(other_fd)

    def test_returns_zero_when_nothing_matches(self, tmp_path):
        assert helpers.set_cloexec_on_device_fds(
            prefix=str(tmp_path / "no-such-device")
        ) == 0
