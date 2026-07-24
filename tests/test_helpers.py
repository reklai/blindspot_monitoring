"""Contracts for the process, device-owner, and health-reporting helpers.

External commands and camera widgets are mocked where their behavior is not
the subject of the test. The close-on-exec tests use real file descriptors
because descriptor inheritance is the behavior they need to protect.
"""

import os
import signal
import subprocess
from unittest import mock

import pytest

from utils import helpers


class TestRunCmd:
    """Define how shell command outcomes are normalized for callers."""

    def test_run_cmd_success(self):
        """A successful command exposes trimmed stdout and a zero status."""
        stdout, stderr, code = helpers.run_cmd("echo hello")
        assert code == 0
        assert stdout == "hello"
        assert stderr == ""

    def test_run_cmd_failure(self):
        """A nonzero child exit remains visible to recovery code."""
        stdout, stderr, code = helpers.run_cmd("false")
        assert code == 1

    def test_run_cmd_timeout(self):
        """A timeout is reported as failure instead of escaping as an exception."""
        stdout, stderr, code = helpers.run_cmd("sleep 10", timeout=1)
        assert code == 1
        assert stdout == ""

    def test_run_cmd_invalid_command(self):
        """An unknown executable produces an error result the caller can inspect."""
        stdout, stderr, code = helpers.run_cmd("nonexistent_command_xyz")
        assert code != 0 or stderr != ""


class TestGetPidsFromLsof:
    """``lsof`` results become validated candidate process IDs."""

    def test_get_pids_empty_when_no_device(self):
        """A missing device is treated as having no holders."""
        pids = helpers.get_pids_from_lsof("/dev/nonexistent_device_xyz")
        assert pids == set()

    @mock.patch("utils.helpers.run_cmd")
    def test_get_pids_parses_output(self, mock_run):
        """One numeric PID per line is converted to a deduplicated integer set."""
        mock_run.return_value = ("1234\n5678\n", "", 0)
        pids = helpers.get_pids_from_lsof("/dev/video0")
        assert pids == {1234, 5678}

    @mock.patch("utils.helpers.run_cmd")
    def test_get_pids_handles_non_numeric(self, mock_run):
        """Diagnostic lines are ignored without discarding valid neighboring PIDs."""
        mock_run.return_value = ("1234\nabc\n5678\n", "", 0)
        pids = helpers.get_pids_from_lsof("/dev/video0")
        assert pids == {1234, 5678}

    @mock.patch("utils.helpers.run_cmd")
    def test_get_pids_returns_empty_on_failure(self, mock_run):
        """A failed ``lsof`` lookup cannot be mistaken for confirmed holders."""
        mock_run.return_value = ("", "error", 1)
        pids = helpers.get_pids_from_lsof("/dev/video0")
        assert pids == set()


class TestGetPidsFromFuser:
    """``fuser`` provides candidate PIDs when ``lsof`` finds no holder."""

    @mock.patch("utils.helpers.run_cmd")
    def test_get_pids_parses_fuser_output(self, mock_run):
        """Extract PID fields from the command's captured standard output."""
        # The helper reads the first ``run_cmd`` result because ``fuser`` writes
        # matching PIDs to stdout; verbose headings may be written to stderr.
        mock_run.return_value = ("/dev/video0: 1234 5678", "", 0)
        pids = helpers.get_pids_from_fuser("/dev/video0")
        assert 1234 in pids
        assert 5678 in pids

    @mock.patch("utils.helpers.run_cmd")
    def test_get_pids_returns_empty_on_failure(self, mock_run):
        """A failed fallback lookup contributes no unverified PIDs."""
        mock_run.return_value = ("", "", 1)
        pids = helpers.get_pids_from_fuser("/dev/video0")
        assert pids == set()


class TestIsPidAlive:
    """Exercise the process-existence probe at both ends of its contract."""

    def test_current_process_is_alive(self):
        """The test process provides a stable positive case on every host."""
        assert helpers.is_pid_alive(os.getpid()) is True

    def test_nonexistent_pid_not_alive(self):
        """An out-of-range practical PID provides a stable negative case."""
        # Linux PID limits are far below this value on supported deployments.
        assert helpers.is_pid_alive(999999999) is False


class TestKillDeviceHolders:
    """Protect the opt-in and escalation rules for reclaiming camera devices."""

    @mock.patch("utils.helpers.get_pids_from_lsof")
    @mock.patch("core.config.KILL_DEVICE_HOLDERS", False)
    def test_disabled_when_config_false(self, mock_lsof):
        """The safety flag prevents even a holder lookup when killing is disabled."""
        result = helpers.kill_device_holders("/dev/video0")
        assert result is False
        mock_lsof.assert_not_called()

    @mock.patch("utils.helpers.get_pids_from_lsof")
    @mock.patch("utils.helpers.get_pids_from_fuser")
    @mock.patch("core.config.KILL_DEVICE_HOLDERS", True)
    def test_returns_false_when_no_holders(self, mock_fuser, mock_lsof):
        """No discovered owner means there was nothing to reclaim."""
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
        """Known holders receive the graceful signal before any harder recovery."""
        fake_pid = 12345
        mock_lsof.return_value = {fake_pid}
        mock_fuser.return_value = set()
        # Model a holder that exits during the grace period, avoiding SIGKILL.
        mock_alive.return_value = False

        result = helpers.kill_device_holders("/dev/video0", grace=0.1)

        assert result is True
        mock_kill.assert_any_call(fake_pid, signal.SIGTERM)


class TestLogHealthSummary:
    """Describe how widget state is reduced to operator-facing health logs."""

    @mock.patch("logging.info")
    @mock.patch("logging.warning")
    def test_logs_health_summary(self, mock_warning, mock_log):
        """Fresh frames count as online in the aggregate summary."""
        import time
        now = time.time()
        
        # Use the minimal widget protocol consumed by ``log_health_summary``;
        # constructing real Qt widgets would obscure the aggregation contract.
        mock_widget1 = mock.MagicMock()
        mock_widget1._latest_frame = "frame_data"
        mock_widget1._last_frame_ts = now
        mock_widget1.worker = None
        mock_widget1.camera_stream_link = 0
        
        mock_widget2 = mock.MagicMock()
        mock_widget2._latest_frame = None
        mock_widget2._last_frame_ts = 0.0
        mock_widget2.worker = None
        mock_widget2.camera_stream_link = 2
        
        mock_widget3 = mock.MagicMock()
        mock_widget3._latest_frame = "frame_data"
        mock_widget3._last_frame_ts = now
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
        # Only widgets with a recent frame contribute to the online count.
        assert call_args[0][1] == 2
    
    @mock.patch("logging.info")
    @mock.patch("logging.warning")
    def test_detects_stale_frames(self, mock_warning, mock_log):
        """An old frame is surfaced separately from a camera with no frame."""
        import time
        now = time.time()
        
        # Fifteen seconds is beyond the helper's freshness window.
        mock_widget = mock.MagicMock()
        mock_widget._latest_frame = "frame_data"
        mock_widget._last_frame_ts = now - 15.0
        mock_widget.worker = None
        mock_widget.camera_stream_link = 0

        helpers.log_health_summary(
            [mock_widget], [], set(), {}
        )
        
        # Match the operator-facing category without coupling to full wording.
        mock_warning.assert_called()
        warning_call = mock_warning.call_args[0][0]
        assert "stale" in warning_call.lower()
    
    @mock.patch("logging.info")
    @mock.patch("logging.warning")
    def test_detects_unhealthy_worker(self, mock_warning, mock_log):
        """Worker health failures remain visible even when a recent frame exists."""
        import time
        now = time.time()
        
        # A fresh frame isolates worker health from the stale-frame warning path.
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
        
        # Match the warning category without freezing incidental formatting.
        mock_warning.assert_called()
        warning_call = mock_warning.call_args[0][0]
        assert "unhealthy" in warning_call.lower()


class TestSetCloexecOnDeviceFds:
    """Protect camera descriptor cleanup across the settings-tile restart.

    OpenCV's V4L2 descriptor may be inheritable, while ``os.execv`` preserves
    the process ID. If the replacement inherited that descriptor,
    ``kill_device_holders`` would skip its own PID and could not reclaim the
    camera. Marking only matching descriptors close-on-exec avoids that leak
    without disturbing unrelated files.
    """

    def test_sets_cloexec_only_on_matching_fds(self, tmp_path):
        device = tmp_path / "video7"
        device.write_bytes(b"")
        other = tmp_path / "unrelated"
        other.write_bytes(b"")

        dev_fd = os.open(device, os.O_RDONLY)
        other_fd = os.open(other, os.O_RDONLY)
        try:
            # Reproduce the descriptor state OpenCV's V4L2 backend can leave.
            os.set_inheritable(dev_fd, True)
            os.set_inheritable(other_fd, True)

            count = helpers.set_cloexec_on_device_fds(
                prefix=str(tmp_path / "video")
            )

            assert count == 1
            # Python exposes the inverse of the underlying ``FD_CLOEXEC`` bit.
            assert os.get_inheritable(dev_fd) is False
            assert os.get_inheritable(other_fd) is True
        finally:
            os.close(dev_fd)
            os.close(other_fd)

    def test_returns_zero_when_nothing_matches(self, tmp_path):
        assert helpers.set_cloexec_on_device_fds(
            prefix=str(tmp_path / "no-such-device")
        ) == 0
