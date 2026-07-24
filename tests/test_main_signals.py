"""Signal-to-Qt shutdown handoff, including the pre-event-loop race.

``main`` is safe to import because startup is guarded by its script entry
point. Tests can therefore exercise signal helpers without running discovery
or entering Qt's event loop.
"""

import signal
from unittest.mock import MagicMock, patch

import main


class TestInstallSignalHandlers:
    """Keep interactive and service shutdown on one cleanup path."""

    def test_registers_sigint_and_sigterm_with_same_handler(self):
        """SIGINT and SIGTERM both feed Qt's ``aboutToQuit`` cleanup chain.

        This makes a terminal interrupt and the service manager's default stop
        signal release camera resources identically.
        """
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
    """Define the handler's immediate and deferred shutdown effects."""

    def test_requests_qapplication_quit(self):
        """Once Qt is running, the handler requests an ordinary event-loop exit."""
        with patch("main.QtWidgets.QApplication.quit") as mock_quit:
            main._handle_shutdown_signal(signal.SIGTERM, None)

        mock_quit.assert_called_once()

    def test_sets_startup_shutdown_flag(self):
        """A signal received before ``app.exec`` remains pending.

        Qt documents ``quit`` as a no-op before the event loop starts. Without
        the flag, a stop during slow camera discovery could be swallowed until
        the service manager resorts to a hard kill.
        """
        main._shutdown_requested["flag"] = False
        try:
            with patch("main.QtWidgets.QApplication.quit"):
                main._handle_shutdown_signal(signal.SIGTERM, None)

            assert main._shutdown_requested["flag"] is True
        finally:
            main._shutdown_requested["flag"] = False

    def test_flag_defaults_false(self):
        """Importing the application does not fabricate a pending shutdown."""
        assert main._shutdown_requested["flag"] is False


class TestStartupShutdownCheck:
    """Close the final race between startup checks and Qt's live event loop.

    ``main`` schedules this helper as the first zero-delay event. Reading the
    flag from inside the running loop catches a signal that arrived after the
    last synchronous check, when an earlier call to ``quit`` was still a no-op.
    """

    def test_quits_when_flag_set(self):
        """The first live-loop checkpoint honors a pending startup signal."""
        main._shutdown_requested["flag"] = True
        try:
            with patch("main.QtWidgets.QApplication.quit") as mock_quit:
                main._startup_shutdown_check()

            mock_quit.assert_called_once()
        finally:
            main._shutdown_requested["flag"] = False

    def test_noop_when_flag_clear(self):
        """Normal startup continues when no signal was recorded."""
        main._shutdown_requested["flag"] = False
        with patch("main.QtWidgets.QApplication.quit") as mock_quit:
            main._startup_shutdown_check()

        mock_quit.assert_not_called()
