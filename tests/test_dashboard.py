"""Tests for main.py - stress hysteresis, rescan plumbing, and tile bookkeeping."""

import threading
import time
from unittest.mock import patch

import pytest
from PyQt6 import QtCore

import main as app_main
from main import Dashboard, StressAction, StressController


class TestStressController:
    def test_requires_consecutive_stress_samples(self):
        c = StressController(stress_hold=3, recover_hold=3)
        assert c.observe(True) is StressAction.NONE
        assert c.observe(True) is StressAction.NONE
        assert c.observe(True) is StressAction.LOWER

    def test_calm_sample_resets_stress_run(self):
        c = StressController(stress_hold=3, recover_hold=3)
        c.observe(True)
        c.observe(True)
        assert c.observe(False) is StressAction.NONE
        assert c.observe(True) is StressAction.NONE
        assert c.observe(True) is StressAction.NONE
        assert c.observe(True) is StressAction.LOWER

    def test_recovery_after_consecutive_calm(self):
        c = StressController(stress_hold=2, recover_hold=2)
        assert c.observe(False) is StressAction.NONE
        assert c.observe(False) is StressAction.RESTORE
        # Counter restarts: another two calm samples for the next RESTORE.
        assert c.observe(False) is StressAction.NONE
        assert c.observe(False) is StressAction.RESTORE

    def test_holds_have_floor_of_one(self):
        c = StressController(stress_hold=0, recover_hold=0)
        assert c.observe(True) is StressAction.LOWER
        assert c.observe(False) is StressAction.RESTORE


@pytest.fixture
def dashboard(qapp, save_restore_config):
    from core import config
    config.CAMERA_SLOT_COUNT = 2
    config.DYNAMIC_FPS_ENABLED = False
    config.HEALTH_LOG_INTERVAL_SEC = 0
    # No real cameras: every slot starts as a placeholder.
    d = Dashboard(qapp, QtCore.QRect(0, 0, 800, 600), working_cameras=[], known_indexes={0, 1})
    yield d
    d.shutdown()


@pytest.mark.usefixtures("qapp")
class TestDashboardLayout:
    def test_builds_settings_plus_slot_tiles(self, dashboard):
        assert dashboard.settings_tile.settings_mode
        assert len(dashboard.placeholder_slots) == 2
        assert dashboard.camera_widgets == []
        assert len(dashboard.all_widgets) == 3
        positions = [t.grid_position for t in dashboard.all_widgets]
        assert positions == [(0, 0), (0, 1), (0, 2)]

    def test_unprobed_devices_start_in_failed_cooldown(self, dashboard):
        assert set(dashboard.failed_indexes) == {0, 1}

    def test_night_mode_and_brightness_fan_out(self, dashboard):
        dashboard.toggle_night_mode()
        assert all(t.night_mode_enabled for t in dashboard.all_widgets)
        assert dashboard.settings_tile._settings.night_mode_button.text() == "Nightmode: On"
        dashboard.set_brightness_all(150)
        assert all(t.brightness == pytest.approx(1.5) for t in dashboard.all_widgets)


@pytest.mark.usefixtures("qapp")
class TestRescan:
    def test_results_cross_from_worker_thread_to_ui_thread(self, qapp, dashboard):
        """The probe runs on an executor thread; results must land on the UI thread."""
        applied = {}

        def apply(results):
            applied["thread"] = threading.current_thread()
            applied["results"] = results

        dashboard._apply_rescan_results = apply  # bypass slot; check delivery only
        dashboard._rescan_finished.disconnect()
        dashboard._rescan_finished.connect(apply)

        class DoneFuture:
            def result(self):
                return [(3, None)]

        worker = threading.Thread(target=dashboard._on_rescan_done, args=(DoneFuture(),))
        worker.start()
        worker.join()
        deadline = time.time() + 2.0
        while "results" not in applied and time.time() < deadline:
            qapp.processEvents()
        assert applied["results"] == [(3, None)]
        assert applied["thread"] is threading.main_thread()

    def test_apply_results_attaches_and_records_failures(self, dashboard, save_restore_config):
        with patch("ui.widgets.CaptureWorker") as worker_cls:
            worker_cls.return_value.isRunning.return_value = False
            dashboard._rescan_inflight = True
            dashboard._apply_rescan_results([(0, None), (1, 1)])
        assert dashboard._rescan_inflight is False
        assert 0 in dashboard.failed_indexes
        assert dashboard.active_indexes == {1}
        assert len(dashboard.camera_widgets) == 1
        assert len(dashboard.placeholder_slots) == 1
        tile = dashboard.camera_widgets[0]
        assert tile.camera_stream_link == 1
        assert tile.capture_enabled
        # The tile is deliberately left holding the mock worker; detach it to
        # keep the fixture teardown honest.
        tile.worker = None
        tile.capture_enabled = False

    def test_candidates_skip_active_and_cooling_down(self, dashboard):
        from core import config
        now = time.time()
        dashboard.active_indexes = {0}
        dashboard.failed_indexes = {1: now, 2: now - config.FAILED_CAMERA_COOLDOWN_SEC - 1}
        with patch("main.get_video_indexes", return_value=[0, 1, 2, 3]):
            assert dashboard._rescan_candidates(now) == [2, 3]

    def test_rescan_is_skipped_while_one_is_in_flight(self, dashboard):
        dashboard._rescan_inflight = True
        with patch("main.get_video_indexes", return_value=[5]), \
             patch.object(dashboard._rescan_executor, "submit") as submit:
            dashboard.rescan_and_attach()
            submit.assert_not_called()

    def test_detach_gives_slot_back(self, dashboard):
        tile = dashboard.placeholder_slots.pop(0)
        dashboard.camera_widgets.append(tile)
        dashboard.active_indexes.add(7)
        tile.capture_enabled = True
        tile.camera_stream_link = 7
        with patch.object(tile, "should_detach", return_value=True):
            dashboard._detach_failed_cameras(time.time())
        assert tile in dashboard.placeholder_slots
        assert tile not in dashboard.camera_widgets
        assert 7 not in dashboard.active_indexes
        assert 7 in dashboard.failed_indexes

    def test_rescan_timer_keeps_running_when_slots_are_full(self, dashboard):
        dashboard.camera_widgets.extend(dashboard.placeholder_slots)
        dashboard.placeholder_slots.clear()
        dashboard.rescan_and_attach()
        assert dashboard.rescan_timer.isActive()


@pytest.mark.usefixtures("qapp")
class TestDynamicFps:
    def test_step_fps_lowers_and_restores_within_bounds(self, dashboard, save_restore_config):
        from core import config
        config.MIN_DYNAMIC_FPS = 10
        config.MIN_DYNAMIC_UI_FPS = 12
        config.UI_FPS_STEP = 2
        tile = dashboard.placeholder_slots[0]
        tile.capture_enabled = True
        tile.base_target_fps = tile.current_target_fps = 14.0
        tile.base_ui_fps = 15
        tile._apply_ui_fps(15)
        dashboard.camera_widgets.append(tile)

        assert dashboard._step_fps(-1)
        assert tile.current_target_fps == 12.0 and tile.ui_render_fps == 13
        assert dashboard._step_fps(-1)
        assert tile.current_target_fps == 10.0 and tile.ui_render_fps == 12
        assert dashboard._step_fps(-1) is False  # already at the floors

        assert dashboard._step_fps(+1)
        assert tile.current_target_fps == 12.0 and tile.ui_render_fps == 14
        assert dashboard._step_fps(+1)
        assert tile.current_target_fps == 14.0 and tile.ui_render_fps == 15
        assert dashboard._step_fps(+1) is False  # back at base
        tile.capture_enabled = False

    def test_adjust_fps_uses_hysteresis(self, dashboard, save_restore_config):
        from core import config
        config.STRESS_HOLD_COUNT = 2
        dashboard.stress = StressController(2, 2)
        with patch.object(app_main, "is_system_stressed", return_value=(True, 0.9, 80.0)), \
             patch.object(dashboard, "_step_fps") as step:
            dashboard.adjust_fps()
            step.assert_not_called()
            dashboard.adjust_fps()
            step.assert_called_once_with(-1)


@pytest.mark.usefixtures("qapp")
class TestShutdown:
    def test_rescan_result_after_shutdown_is_dropped(self, dashboard):
        received = []
        dashboard._rescan_finished.connect(received.append)
        dashboard.shutdown()

        class DoneFuture:
            def result(self):
                return [(1, 1)]

        dashboard._on_rescan_done(DoneFuture())
        assert received == []

    def test_shutdown_is_idempotent_and_stops_timers(self, dashboard):
        dashboard.shutdown()
        dashboard.shutdown()
        assert not dashboard.rescan_timer.isActive()
