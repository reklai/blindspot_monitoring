"""
Tests for core/camera.py pure slot-assignment functions.

Covers assign_slots() (bulk startup assignment) and
choose_slot_for_identity() (rescan/reattach single-identity assignment).
Both are pure functions: plain CameraIdentity construction, no Qt/cv2
mocking needed. SAFETY RULE under test throughout: a pinned slot must
never surface a different camera than its pin -- an empty/reserved tile
beats the wrong camera.
"""

import logging

from core.camera import CameraIdentity, assign_slots, choose_slot_for_identity


PORT_A = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0"
PORT_B = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.4:1.0"
PORT_C = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.10:1.0"


class TestAssignSlots:
    """assign_slots(identities, slot_count, pins) -> list[Optional[CameraIdentity]]."""

    def test_default_port_order_fills_slots_in_order(self):
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)
        b = CameraIdentity(PORT_B, f"/dev/v4l/by-path/{PORT_B}-video-index0", 1)

        result = assign_slots([a, b], slot_count=3, pins={})

        assert result == [a, b, None]

    def test_fewer_cameras_than_slots_leaves_trailing_none(self):
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)

        result = assign_slots([a], slot_count=3, pins={})

        assert result == [a, None, None]

    def test_extra_cameras_are_dropped_and_logged(self, caplog):
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)
        b = CameraIdentity(PORT_B, f"/dev/v4l/by-path/{PORT_B}-video-index0", 1)
        c = CameraIdentity(PORT_C, f"/dev/v4l/by-path/{PORT_C}-video-index0", 2)

        with caplog.at_level(logging.INFO):
            result = assign_slots([a, b, c], slot_count=2, pins={})

        assert result == [a, b]
        assert any(
            r.levelno == logging.INFO and "dropped" in r.getMessage()
            for r in caplog.records
        )

    def test_pin_beats_order(self):
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)
        b = CameraIdentity(PORT_B, f"/dev/v4l/by-path/{PORT_B}-video-index0", 1)

        # Without a pin, discovery order would put b in slot 0. Pin slot 0
        # to PORT_B's substring to force it there regardless of order.
        result = assign_slots([a, b], slot_count=2, pins={0: "1.4"})

        assert result[0] == b
        assert result[1] == a

    def test_pinned_port_absent_stays_none_even_with_spare_cameras(self):
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)
        b = CameraIdentity(PORT_B, f"/dev/v4l/by-path/{PORT_B}-video-index0", 1)

        # slot0 pinned to a port that doesn't match any discovered camera.
        # It must stay None -- NEVER backfilled by a and b, even though
        # there are spare unpinned cameras available.
        result = assign_slots([a, b], slot_count=3, pins={0: "1.99"})

        assert result[0] is None
        assert a in result
        assert b in result

    def test_ambiguous_pin_picks_natural_first_and_warns(self, caplog):
        # Both port paths contain the substring "1.3", so a pin of "1.3"
        # matches both -- ambiguous. Natural-sort-first (PORT_A, "1.3")
        # must win over the "1.30" port.
        port_wide = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.30:1.0"
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)
        wide = CameraIdentity(port_wide, f"/dev/v4l/by-path/{port_wide}-video-index0", 1)

        with caplog.at_level(logging.WARNING):
            result = assign_slots([wide, a], slot_count=1, pins={0: "usb-0:1.3"})

        assert result == [a]
        assert any("ambiguous pin" in r.getMessage() for r in caplog.records)

    def test_index_pin_exact_matches_and_does_not_substring_match(self):
        ident1 = CameraIdentity("index:1", None, 1)
        ident10 = CameraIdentity("index:10", None, 10)

        # pin "index:1" must NOT claim "index:10" via substring matching.
        result = assign_slots([ident1, ident10], slot_count=2, pins={0: "index:1"})

        assert result[0] == ident1
        assert ident10 in result
        assert result[1] == ident10

    def test_pin_claimed_identity_not_duplicated_in_unpinned_fill(self):
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)
        b = CameraIdentity(PORT_B, f"/dev/v4l/by-path/{PORT_B}-video-index0", 1)

        result = assign_slots([a, b], slot_count=3, pins={0: "1.3"})

        # a claimed by pin at slot 0; must not also appear via unpinned fill.
        assert result[0] == a
        assert result.count(a) == 1
        assert b in result

    def test_returns_exactly_slot_count_entries(self):
        result = assign_slots([], slot_count=3, pins={})
        assert len(result) == 3
        assert result == [None, None, None]


class TestChooseSlotForIdentity:
    """choose_slot_for_identity(identity, free_slot_indexes, pins, last_slot_by_port)."""

    def test_prefers_matching_pinned_free_slot(self):
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)

        slot = choose_slot_for_identity(
            a, free_slot_indexes=[0, 1, 2], pins={1: "1.3"}, last_slot_by_port={}
        )

        assert slot == 1

    def test_returns_to_last_slot_after_replug_index_independent(self):
        # Same port, different index (simulating a replug that got a new
        # /dev/videoN number) -- last_slot_by_port lookup is by port_path,
        # so it must be honored regardless of the new index.
        ident_before = CameraIdentity(PORT_A, "/dev/v4l/by-path/x-video-index0", 0)
        ident_after = CameraIdentity(PORT_A, "/dev/v4l/by-path/x-video-index0", 7)
        assert ident_before.index != ident_after.index

        slot = choose_slot_for_identity(
            ident_after,
            free_slot_indexes=[0, 1, 2],
            pins={},
            last_slot_by_port={PORT_A: 2},
        )

        assert slot == 2

    def test_last_slot_skipped_when_now_pinned_to_different_port(self):
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)

        # Slot 2 was a's last slot, but it's now pinned to PORT_B -- a must
        # not steal it. Falls through to lowest free unpinned slot.
        slot = choose_slot_for_identity(
            a,
            free_slot_indexes=[0, 1, 2],
            pins={2: "1.4"},
            last_slot_by_port={PORT_A: 2},
        )

        assert slot == 0

    def test_falls_back_to_lowest_free_unpinned_slot(self):
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)

        slot = choose_slot_for_identity(
            a, free_slot_indexes=[1, 2], pins={}, last_slot_by_port={}
        )

        assert slot == 1

    def test_returns_none_when_only_non_matching_pinned_slots_are_free(self):
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)

        slot = choose_slot_for_identity(
            a,
            free_slot_indexes=[1, 2],
            pins={1: "1.4", 2: "1.10"},
            last_slot_by_port={},
        )

        assert slot is None
