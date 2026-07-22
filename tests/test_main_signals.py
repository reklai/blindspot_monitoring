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
