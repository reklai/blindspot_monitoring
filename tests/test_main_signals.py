"""
Tests for main._install_signal_handlers / _handle_shutdown_signal.

Importing main is side-effect free (main() is guarded by
`if __name__ == "__main__"`), which lets these unit tests import it
without pulling in Qt event-loop or camera-discovery side effects (see
tests/test_main_rescan.py for the same rationale).
"""

import signal
from unittest.mock import MagicMock, patch

import main


class TestInstallSignalHandlers:
    def test_registers_sigint_and_sigterm_with_same_handler(self):
        """Both SIGINT (Ctrl+C) and SIGTERM (systemd `stop`, KillSignal
        default per install.sh) must route through the same shutdown
        handler so a service stop follows the same
        QApplication.quit() -> aboutToQuit -> safe_cleanup path as Ctrl+C."""
        fake_app = MagicMock()

        with patch("main.signal.signal") as mock_signal:
            main._install_signal_handlers(fake_app)

        assert mock_signal.call_count == 2
        registered = {
            call.args[0]: call.args[1] for call in mock_signal.call_args_list
        }
        assert set(registered.keys()) == {signal.SIGINT, signal.SIGTERM}
        assert registered[signal.SIGINT] is main._handle_shutdown_signal
        assert registered[signal.SIGTERM] is main._handle_shutdown_signal


class TestHandleShutdownSignal:
    def test_requests_qapplication_quit(self):
        """The shared handler requests a clean shutdown via
        QApplication.quit()."""
        with patch("main.QtWidgets.QApplication.quit") as mock_quit:
            main._handle_shutdown_signal(signal.SIGTERM, None)

        mock_quit.assert_called_once()

    def test_sets_startup_shutdown_flag(self):
        """QApplication.quit() is a documented no-op before app.exec()
        starts, so a SIGTERM during the multi-second startup camera
        discovery would otherwise be silently swallowed (systemd then waits
        out TimeoutStopSec and SIGKILLs mid-capture). The handler must ALSO
        set a flag that main()'s startup checkpoints read."""
        main._shutdown_requested["flag"] = False
        try:
            with patch("main.QtWidgets.QApplication.quit"):
                main._handle_shutdown_signal(signal.SIGTERM, None)

            assert main._shutdown_requested["flag"] is True
        finally:
            main._shutdown_requested["flag"] = False

    def test_flag_defaults_false(self):
        """Fresh import: no shutdown pending."""
        assert main._shutdown_requested["flag"] is False


class TestStartupShutdownCheck:
    """_startup_shutdown_check is scheduled with QTimer.singleShot(0, ...)
    right before app.exec() and runs as the first event of the live loop.

    A plain pre-exec `if flag` check leaves a race: a signal landing between
    that check and the loop actually starting calls quit() while it is still
    a documented no-op, and the flag would never be read again -- the
    swallowed-shutdown bug survives in that window. Re-reading the flag from
    INSIDE the running loop closes it: from there quit() works, and any
    earlier signal left the flag set."""

    def test_quits_when_flag_set(self):
        main._shutdown_requested["flag"] = True
        try:
            with patch("main.QtWidgets.QApplication.quit") as mock_quit:
                main._startup_shutdown_check()

            mock_quit.assert_called_once()
        finally:
            main._shutdown_requested["flag"] = False

    def test_noop_when_flag_clear(self):
        main._shutdown_requested["flag"] = False
        with patch("main.QtWidgets.QApplication.quit") as mock_quit:
            main._startup_shutdown_check()

        mock_quit.assert_not_called()
