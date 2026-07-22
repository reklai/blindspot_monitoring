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

from core.camera import CameraIdentity
from main import _plan_detach_sweep, _plan_rescan_attachments


PORT_A = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0"
PORT_B = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.4:1.0"


class _FakeWidget:
    """Minimal stand-in for a CameraWidget for sweep-decision tests."""

    def __init__(self, capture_enabled: bool, permanently_failed: bool):
        self.capture_enabled = capture_enabled
        self._permanently_failed = permanently_failed
        self.seen_now = None

    def is_permanently_failed(self, now: float) -> bool:
        self.seen_now = now
        return self._permanently_failed


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
