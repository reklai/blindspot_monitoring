"""
Tests for main._plan_rescan_attachments -- the pure-ish rescan planner.

The planner decides WHICH probed identity attaches to WHICH slot (and
which ports get marked failed), leaving the Qt attach side in main()'s
closure. Importing main must be side-effect free (main() is guarded by
`if __name__ == "__main__"`), which lets these unit tests import it.

SAFETY RULES under test:
  - a replugged camera (same port, new /dev/videoN index) returns to ITS
    previous slot via last_slot_by_port;
  - a pinned port returns to its pinned slot even when another slot is free;
  - a non-matching camera never lands in a reserved pinned slot;
  - a reserved-slot wait (no slot available) is NOT a failure.
"""

from unittest.mock import patch

import pytest

from core.camera import CameraIdentity
from main import _plan_detach_sweep, _plan_rescan_attachments, _run_rescan_tests


PORT_A = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0"
PORT_B = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.4:1.0"


@pytest.fixture
def fake_by_path(tmp_path, monkeypatch):
    """Build a fake /dev/v4l/by-path tree (mirrors test_camera_identity)."""
    dev_dir = tmp_path / "dev"
    dev_dir.mkdir()
    by_path_dir = dev_dir / "by-path"
    by_path_dir.mkdir()

    def make_link(entry_name: str, video_index: int):
        node = dev_dir / f"video{video_index}"
        if not node.exists():
            node.touch()
        (by_path_dir / entry_name).symlink_to(node)

    import core.camera as camera_module

    monkeypatch.setattr(camera_module, "BY_PATH_DIR", str(by_path_dir))
    monkeypatch.setattr(camera_module, "_by_path_degraded_warned", False)

    return {"dev_dir": dev_dir, "by_path_dir": by_path_dir, "make_link": make_link}


class TestRunRescanTests:
    """Finding 2: the rescan reattach probe must not give up after only the
    group's provisional (lowest) node fails -- it falls back to probing the
    group's remaining nodes, exactly as startup does, so a camera whose
    capture node isn't the lowest can still hot-plug reattach.
    """

    def test_fallback_probes_higher_node_when_lowest_fails(self, fake_by_path):
        by_path_dir = fake_by_path["by_path_dir"]
        make_link = fake_by_path["make_link"]
        make_link(f"{PORT_A}-video-index0", 0)  # metadata node, fails grab()
        make_link(f"{PORT_A}-video-index1", 1)  # real capture node

        # Provisional identity from discover_camera_identities = lowest node.
        provisional = CameraIdentity(
            PORT_A, str(by_path_dir / f"{PORT_A}-video-index0"), 0
        )

        def fake_test(target, **kw):
            # Fast path probes the provisional device_path (node0) -> fails.
            if isinstance(target, str):
                return None
            # Fallback probes remaining group nodes by index -> node1 works.
            return target if target == 1 else None

        with patch("main.test_single_camera", side_effect=fake_test), patch(
            "core.camera.test_single_camera", side_effect=fake_test
        ):
            results = _run_rescan_tests([provisional])

        assert len(results) == 1
        identity, resolved_index = results[0]
        assert resolved_index == 1
        assert identity.port_path == PORT_A
        assert identity.index == 1
        assert identity.device_path == str(by_path_dir / f"{PORT_A}-video-index1")

    def test_fast_path_success_skips_fallback(self, fake_by_path):
        by_path_dir = fake_by_path["by_path_dir"]
        make_link = fake_by_path["make_link"]
        make_link(f"{PORT_A}-video-index0", 0)

        provisional = CameraIdentity(
            PORT_A, str(by_path_dir / f"{PORT_A}-video-index0"), 0
        )

        with patch("main.test_single_camera", return_value=0) as fast, patch(
            "core.camera.test_single_camera"
        ) as fallback:
            results = _run_rescan_tests([provisional])

        assert results == [(provisional, 0)]
        assert fast.call_count == 1
        fallback.assert_not_called()

    def test_fallback_identity_has_no_group_expansion(self):
        # An index:N fallback identity (device_path None) has a single node;
        # a failed probe stays failed, no group to expand.
        ident = CameraIdentity("index:5", None, 5)

        with patch("main.test_single_camera", return_value=None):
            results = _run_rescan_tests([ident])

        assert results == [(ident, None)]


class _FakeWidget:
    """Minimal stand-in for a CameraWidget for sweep-decision tests."""

    def __init__(self, capture_enabled: bool, permanently_failed: bool):
        self.capture_enabled = capture_enabled
        self._permanently_failed = permanently_failed
        self.seen_now = None

    def is_permanently_failed(self, now: float) -> bool:
        self.seen_now = now
        return self._permanently_failed


class TestRescanResultBridge:
    """Rescan results must be marshalled from the executor thread to the GUI
    thread through a QObject signal. The previous relay --
    QTimer.singleShot(0, ...) called inside future.add_done_callback -- runs
    in the executor thread, where the timer has no event loop and NEVER
    fires: results were dropped and rescan_inflight stuck True forever,
    killing hot-plug attach after the first probe.
    """

    def test_results_delivered_on_gui_thread_from_executor_callback(self, qapp):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        import main as main_mod

        bridge = main_mod._RescanBridge()
        received = []
        receiver_threads = []

        def receiver(results):
            received.append(results)
            receiver_threads.append(threading.current_thread())

        bridge.results_ready.connect(receiver)

        payload = [("identity-sentinel", 3)]
        gate = threading.Event()
        done = threading.Event()

        def probe_task():
            # Block until the done-callback is registered, so the callback
            # provably runs on the executor thread (an already-finished
            # future would run it inline on this test's main thread).
            assert gate.wait(2.0)
            return payload

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(probe_task)
            callback = main_mod._make_rescan_done_callback(bridge)

            def wrapped(fut):
                callback(fut)
                done.set()

            future.add_done_callback(wrapped)
            gate.set()
            assert done.wait(2.0)

            # Queued cross-thread delivery: nothing may arrive until the GUI
            # event loop runs.
            assert received == []
            qapp.processEvents()

            assert received == [payload]
            assert receiver_threads == [threading.main_thread()]
        finally:
            executor.shutdown(wait=True)

    def test_failed_probe_task_delivers_empty_results(self, qapp):
        from concurrent.futures import ThreadPoolExecutor

        import main as main_mod

        bridge = main_mod._RescanBridge()
        received = []
        bridge.results_ready.connect(received.append)

        def failing_task():
            raise RuntimeError("probe blew up")

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(failing_task)
            future.exception(timeout=2.0)  # wait for completion
            # Callback on a finished future runs inline (same thread) --
            # fine here; the exception path is what's under test.
            future.add_done_callback(main_mod._make_rescan_done_callback(bridge))
            qapp.processEvents()

            assert received == [[]]
        finally:
            executor.shutdown(wait=True)


class TestPlanDetachSweep:
    """The detach sweep (Finding 1) must run every tick regardless of whether
    any placeholder slots are free -- a widget that leaked its worker in the
    deployed all-slots-full steady state must still be detached.
    """

    def test_permanently_failed_capture_widget_is_swept(self):
        w = _FakeWidget(capture_enabled=True, permanently_failed=True)
        assert _plan_detach_sweep([w], now=100.0) == [w]
        assert w.seen_now == 100.0

    def test_healthy_capture_widget_is_not_swept(self):
        w = _FakeWidget(capture_enabled=True, permanently_failed=False)
        assert _plan_detach_sweep([w], now=100.0) == []

    def test_placeholder_widget_is_not_swept(self):
        # capture_enabled False -> a placeholder/detached slot, never a
        # detach candidate even if is_permanently_failed would say True.
        w = _FakeWidget(capture_enabled=False, permanently_failed=True)
        assert _plan_detach_sweep([w], now=100.0) == []

    def test_only_failed_widgets_selected_from_mixed_list(self):
        healthy = _FakeWidget(capture_enabled=True, permanently_failed=False)
        failed = _FakeWidget(capture_enabled=True, permanently_failed=True)
        placeholder = _FakeWidget(capture_enabled=False, permanently_failed=True)
        assert _plan_detach_sweep(
            [healthy, failed, placeholder], now=42.0
        ) == [failed]


def _identity(port: str, index: int) -> CameraIdentity:
    return CameraIdentity(port, f"/dev/v4l/by-path/{port}-video-index0", index)


class TestPlanRescanAttachments:
    def test_replug_returns_to_same_slot_via_last_slot_memory(self):
        # PORT_A was previously in slot 1, then unplugged (detach bookkeeping
        # already applied: dropped from active_ports, kept in last_slot_by_port).
        last_slot_by_port = {PORT_A: 1}
        active_ports: set[str] = set()
        failed_ports: dict[str, float] = {}
        # Reappears with a DIFFERENT resolved index (5).
        ident = _identity(PORT_A, 5)

        attachments = _plan_rescan_attachments(
            results=[(ident, 5)],
            free_slot_indexes=[0, 1, 2],  # all free; lowest is 0
            pins={},
            last_slot_by_port=last_slot_by_port,
            active_ports=active_ports,
            failed_ports=failed_ports,
            now=100.0,
        )

        # Returns to its remembered slot 1, not the lowest free slot 0.
        assert attachments == [(ident, 1)]
        assert active_ports == {PORT_A}
        assert last_slot_by_port[PORT_A] == 1
        assert PORT_A not in failed_ports

    def test_pinned_port_returns_to_pinned_slot_even_with_free_slot(self):
        # Pin slot 2 to PORT_A's port tail; slot 0 is also free.
        ident = _identity(PORT_A, 3)
        active_ports: set[str] = set()
        last_slot_by_port: dict[str, int] = {}
        failed_ports: dict[str, float] = {}

        attachments = _plan_rescan_attachments(
            results=[(ident, 3)],
            free_slot_indexes=[0, 2],
            pins={2: "usb-0:1.3"},
            last_slot_by_port=last_slot_by_port,
            active_ports=active_ports,
            failed_ports=failed_ports,
            now=100.0,
        )

        assert attachments == [(ident, 2)]
        assert last_slot_by_port[PORT_A] == 2

    def test_non_matching_camera_never_takes_reserved_pinned_slot(self):
        # Slot 0 pinned to a port PORT_A does not match; slot 1 free & unpinned.
        ident = _identity(PORT_A, 3)
        active_ports: set[str] = set()
        last_slot_by_port: dict[str, int] = {}
        failed_ports: dict[str, float] = {}

        attachments = _plan_rescan_attachments(
            results=[(ident, 3)],
            free_slot_indexes=[0, 1],
            pins={0: "usb-9:9.9"},
            last_slot_by_port=last_slot_by_port,
            active_ports=active_ports,
            failed_ports=failed_ports,
            now=100.0,
        )

        # Lands in the free unpinned slot 1, never the reserved slot 0.
        assert attachments == [(ident, 1)]

    def test_reserved_slot_wait_is_not_marked_failed(self):
        # Only free slot is 0, pinned to a port PORT_A does not match.
        ident = _identity(PORT_A, 3)
        active_ports: set[str] = set()
        last_slot_by_port: dict[str, int] = {}
        failed_ports: dict[str, float] = {}

        attachments = _plan_rescan_attachments(
            results=[(ident, 3)],
            free_slot_indexes=[0],
            pins={0: "usb-9:9.9"},
            last_slot_by_port=last_slot_by_port,
            active_ports=active_ports,
            failed_ports=failed_ports,
            now=100.0,
        )

        # Camera waits: no attachment, and NOT marked failed (retried next tick).
        assert attachments == []
        assert PORT_A not in failed_ports
        assert PORT_A not in active_ports

    def test_failed_probe_marks_port_failed_with_timestamp(self):
        ident = _identity(PORT_A, 3)
        failed_ports: dict[str, float] = {}

        attachments = _plan_rescan_attachments(
            results=[(ident, None)],  # probe failed
            free_slot_indexes=[0, 1, 2],
            pins={},
            last_slot_by_port={},
            active_ports=set(),
            failed_ports=failed_ports,
            now=123.0,
        )

        assert attachments == []
        assert failed_ports[PORT_A] == 123.0

    def test_successful_probe_clears_prior_cooldown(self):
        ident = _identity(PORT_A, 3)
        failed_ports = {PORT_A: 50.0}

        attachments = _plan_rescan_attachments(
            results=[(ident, 3)],
            free_slot_indexes=[0],
            pins={},
            last_slot_by_port={},
            active_ports=set(),
            failed_ports=failed_ports,
            now=100.0,
        )

        assert attachments == [(ident, 0)]
        assert PORT_A not in failed_ports

    def test_batch_consumes_free_slots_without_double_assign(self):
        a = _identity(PORT_A, 3)
        b = _identity(PORT_B, 4)
        last_slot_by_port: dict[str, int] = {}
        active_ports: set[str] = set()

        attachments = _plan_rescan_attachments(
            results=[(a, 3), (b, 4)],
            free_slot_indexes=[1, 2],
            pins={},
            last_slot_by_port=last_slot_by_port,
            active_ports=active_ports,
            failed_ports={},
            now=100.0,
        )

        slots = [slot for _, slot in attachments]
        assert sorted(slots) == [1, 2]
        assert len(set(slots)) == 2  # no double assignment
        assert active_ports == {PORT_A, PORT_B}

    def test_already_active_port_is_skipped(self):
        ident = _identity(PORT_A, 3)
        active_ports = {PORT_A}
        failed_ports: dict[str, float] = {}

        attachments = _plan_rescan_attachments(
            results=[(ident, 3)],
            free_slot_indexes=[0, 1],
            pins={},
            last_slot_by_port={},
            active_ports=active_ports,
            failed_ports=failed_ports,
            now=100.0,
        )

        assert attachments == []
        assert PORT_A not in failed_ports


class TestQuiesceRescanExecutor:
    """Restart must leave no probe thread able to open a camera fd.

    The settings-tile restart marks open camera descriptors close-on-exec in
    a single scan before ``execv``. A probe still running in the rescan
    executor could open ``/dev/video*`` after that scan, and the inherited
    descriptor would be unrecoverable because holder cleanup excludes the
    process's own PID. Quiescing must therefore join in-flight probes and
    drop queued ones before the scan runs.
    """

    def test_joins_inflight_probe_before_returning(self):
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor

        from main import _quiesce_rescan_executor

        executor = ThreadPoolExecutor(max_workers=1)
        finished = threading.Event()

        def slow_probe():
            time.sleep(0.2)
            finished.set()

        executor.submit(slow_probe)
        _quiesce_rescan_executor(executor)

        assert finished.is_set()

    def test_drops_queued_probe_batch_instead_of_running_it(self):
        import time
        from concurrent.futures import ThreadPoolExecutor

        from main import _quiesce_rescan_executor

        executor = ThreadPoolExecutor(max_workers=1)
        executor.submit(time.sleep, 0.2)
        queued = executor.submit(time.sleep, 0.2)

        _quiesce_rescan_executor(executor)

        assert queued.cancelled()
