"""Background rescan, detach, and attachment-planning contracts.

The planner converts probe results into slot decisions while the Qt closure in
``main`` performs the actual attachment. Tests protect USB port path placement,
pin reservations, retry bookkeeping, and the cross-thread signal that returns
probe results to the GUI. Importing ``main`` is safe because its entry point is
guarded and does not start discovery.
"""

from unittest.mock import patch

import pytest

from core.camera import CameraIdentity
from main import _plan_detach_sweep, _plan_rescan_attachments, _run_rescan_tests


PORT_A = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0"
PORT_B = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.4:1.0"


@pytest.fixture
def fake_by_path(tmp_path, monkeypatch):
    """Provide the same isolated udev-like tree used by identity tests.

    Rescan fallback reads ``core.camera.BY_PATH_DIR`` directly, so redirecting
    it is required even though the planner itself lives in ``main``.
    """
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
    """Keep hot-plug probing aligned with startup's USB port path group search.

    Cheap discovery proposes a group's lowest node. A metadata-only lowest
    node must not hide a usable sibling during rescan.
    """

    def test_fallback_probes_higher_node_when_lowest_fails(self, fake_by_path):
        """A failed provisional node falls through to the group's capture node."""
        by_path_dir = fake_by_path["by_path_dir"]
        make_link = fake_by_path["make_link"]
        make_link(f"{PORT_A}-video-index0", 0)  # Enumerated first, but metadata-only.
        make_link(f"{PORT_A}-video-index1", 1)  # Usable capture sibling.

        # Cheap discovery always supplies the group's lowest node initially.
        provisional = CameraIdentity(
            PORT_A, str(by_path_dir / f"{PORT_A}-video-index0"), 0
        )

        def fake_test(target, **kw):
            # String input identifies the provisional by-path fast path.
            if isinstance(target, str):
                return None
            # Numeric input identifies the sibling-node fallback.
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
        """A usable provisional node avoids redundant group expansion."""
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
        """A degraded numeric identity has no by-path group to expand."""
        ident = CameraIdentity("index:5", None, 5)

        with patch("main.test_single_camera", return_value=None):
            results = _run_rescan_tests([ident])

        assert results == [(ident, None)]


class _FakeWidget:
    """Expose only the state and method consumed by the detach planner."""

    def __init__(self, capture_enabled: bool, permanently_failed: bool):
        self.capture_enabled = capture_enabled
        self._permanently_failed = permanently_failed
        self.seen_now = None

    def is_permanently_failed(self, now: float) -> bool:
        self.seen_now = now
        return self._permanently_failed


class TestRescanResultBridge:
    """Protect delivery from the probe executor back to Qt's GUI thread.

    A prior relay created a zero-delay timer inside the executor callback.
    Because that thread had no Qt event loop, the timer never fired,
    ``rescan_inflight`` stayed true, and hot-plug attachment stopped after the
    first probe. A QObject signal provides the required queued delivery.
    """

    def test_results_delivered_on_gui_thread_from_executor_callback(self, qapp):
        """Executor completion is queued until the GUI processes events."""
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
            # Hold completion until callback registration. ``Future`` otherwise
            # invokes callbacks for an already-finished task on the registering
            # thread, which would not exercise cross-thread delivery.
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

            # An empty collector before ``processEvents`` proves delivery was queued.
            assert received == []
            qapp.processEvents()

            assert received == [payload]
            assert receiver_threads == [threading.main_thread()]
        finally:
            executor.shutdown(wait=True)

    def test_failed_probe_task_delivers_empty_results(self, qapp):
        """A background exception becomes an empty result batch for the GUI."""
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
            # Finish first so this case isolates exception translation rather
            # than repeating the cross-thread scheduling assertion above.
            future.exception(timeout=2.0)
            future.add_done_callback(main_mod._make_rescan_done_callback(bridge))
            qapp.processEvents()

            assert received == [[]]
        finally:
            executor.shutdown(wait=True)


class TestPlanDetachSweep:
    """Select permanently failed capture widgets independently of free slots.

    In the all-slots-full steady state, a leaked worker still needs detaching;
    tying the sweep to placeholder availability would leave it stuck forever.
    """

    def test_permanently_failed_capture_widget_is_swept(self):
        """An active widget past permanent-failure policy becomes a detach target."""
        w = _FakeWidget(capture_enabled=True, permanently_failed=True)
        assert _plan_detach_sweep([w], now=100.0) == [w]
        assert w.seen_now == 100.0

    def test_healthy_capture_widget_is_not_swept(self):
        """An active widget still within recovery policy remains attached."""
        w = _FakeWidget(capture_enabled=True, permanently_failed=False)
        assert _plan_detach_sweep([w], now=100.0) == []

    def test_placeholder_widget_is_not_swept(self):
        """Detached placeholders never re-enter the detach path."""
        # Set permanent failure true to prove ``capture_enabled`` is the earlier guard.
        w = _FakeWidget(capture_enabled=False, permanently_failed=True)
        assert _plan_detach_sweep([w], now=100.0) == []

    def test_only_failed_widgets_selected_from_mixed_list(self):
        """A mixed sweep preserves only active, permanently failed widgets."""
        healthy = _FakeWidget(capture_enabled=True, permanently_failed=False)
        failed = _FakeWidget(capture_enabled=True, permanently_failed=True)
        placeholder = _FakeWidget(capture_enabled=False, permanently_failed=True)
        assert _plan_detach_sweep(
            [healthy, failed, placeholder], now=42.0
        ) == [failed]


def _identity(port: str, index: int) -> CameraIdentity:
    return CameraIdentity(port, f"/dev/v4l/by-path/{port}-video-index0", index)


class TestPlanRescanAttachments:
    """Define state transitions when successful and failed probes are planned."""

    def test_replug_returns_to_same_slot_via_last_slot_memory(self):
        """The same USB port path returns to its slot after index reassignment."""
        # Detach removes the active marker but deliberately retains slot memory.
        last_slot_by_port = {PORT_A: 1}
        active_ports: set[str] = set()
        failed_ports: dict[str, float] = {}
        # A different index reproduces udev numbering after a replug.
        ident = _identity(PORT_A, 5)

        attachments = _plan_rescan_attachments(
            results=[(ident, 5)],
            free_slot_indexes=[0, 1, 2],
            pins={},
            last_slot_by_port=last_slot_by_port,
            active_ports=active_ports,
            failed_ports=failed_ports,
            now=100.0,
        )

        # Slot 0 is also free, so choosing 1 specifically proves memory won.
        assert attachments == [(ident, 1)]
        assert active_ports == {PORT_A}
        assert last_slot_by_port[PORT_A] == 1
        assert PORT_A not in failed_ports

    def test_pinned_port_returns_to_pinned_slot_even_with_free_slot(self):
        """A matching reservation outranks a lower unpinned slot."""
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
        """A returning camera bypasses reservations for other USB port paths."""
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

        assert attachments == [(ident, 1)]

    def test_reserved_slot_wait_is_not_marked_failed(self):
        """Lack of an eligible tile is a wait condition, not a camera failure."""
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

        # Leaving both maps untouched permits another attempt on the next tick.
        assert attachments == []
        assert PORT_A not in failed_ports
        assert PORT_A not in active_ports

    def test_failed_probe_marks_port_failed_with_timestamp(self):
        """A real probe failure starts that port's retry cooldown."""
        ident = _identity(PORT_A, 3)
        failed_ports: dict[str, float] = {}

        attachments = _plan_rescan_attachments(
            results=[(ident, None)],
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
        """Recovery removes stale failure bookkeeping before attachment."""
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
        """Planning a batch consumes each free slot at most once."""
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
        # Set cardinality makes accidental reuse explicit even if order changes.
        assert len(set(slots)) == 2
        assert active_ports == {PORT_A, PORT_B}

    def test_already_active_port_is_skipped(self):
        """Duplicate discovery of an attached port causes no state transition."""
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
