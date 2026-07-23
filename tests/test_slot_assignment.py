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

from core.camera import (
    CameraIdentity,
    _pin_matches,
    assign_slots,
    choose_slot_for_identity,
)


PORT_A = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0"
PORT_B = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.4:1.0"
PORT_C = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.10:1.0"


class TestPinMatches:
    """_pin_matches must match port-tail pins only at component boundaries.

    Plain substring matching lets pin "usb-0:1.1" claim ports "usb-0:1.10"
    (10+ port hub) and "usb-0:1.1.2" (chained hub) -- the wrong physical
    camera in a pinned, safety-relevant tile. A match must therefore end at
    ':' (the interface suffix) or end-of-string, and start at a component
    boundary.
    """

    def test_port_tail_pin_matches_its_own_port(self):
        assert _pin_matches("usb-0:1.3", PORT_A) is True

    def test_bare_fragment_pin_matches_at_boundaries(self):
        assert _pin_matches("1.4", PORT_B) is True

    def test_full_port_path_pin_matches_exactly(self):
        assert _pin_matches(PORT_A, PORT_A) is True

    def test_pin_does_not_match_longer_port_number(self):
        # "usb-0:1.1" must not claim "usb-0:1.10".
        port_10 = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.10:1.0"
        assert _pin_matches("usb-0:1.1", port_10) is False

    def test_pin_does_not_match_chained_hub_subport(self):
        # "usb-0:1.1" must not claim "usb-0:1.1.2" (hub behind port 1.1).
        port_chained = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.1.2:1.0"
        assert _pin_matches("usb-0:1.1", port_chained) is False

    def test_bare_fragment_pin_does_not_match_chained_hub_tail(self):
        # SAFETY: bare pin "1.1" must not claim "usb-0:2.1.1" (hub in port
        # 2.1, camera in its port 1). A match may not begin right after '.'
        # -- that is the inside of a dotted port number, so the fragment
        # would only cover the tail of a DIFFERENT physical port.
        port_chained_tail = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:2.1.1:1.0"
        assert _pin_matches("1.1", port_chained_tail) is False

    def test_bare_fragment_pin_still_matches_whole_port_number(self):
        # The supported bare-fragment form keeps working when it covers the
        # WHOLE port number (preceded by ':').
        port_11 = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.1:1.0"
        assert _pin_matches("1.1", port_11) is True

    def test_pin_does_not_match_starting_mid_token(self):
        # A match may not begin in the middle of a component ("...usb-0:1.3"
        # contains "b-0:1.3" but that is not a port tail).
        assert _pin_matches("b-0:1.3", PORT_A) is False

    def test_index_pin_requires_exact_match(self):
        assert _pin_matches("index:1", "index:1") is True
        assert _pin_matches("index:1", "index:10") is False


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
        # The same "usb-0:1.3" port tail exists on two different USB
        # controllers, so the pin genuinely matches both -- ambiguous.
        # Natural-sort-first (PORT_A, "...fd500000...") must win over the
        # "...xhci..." port.
        port_other_bus = "platform-xhci-hcd.1-usb-0:1.3:1.0"
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)
        other = CameraIdentity(
            port_other_bus, f"/dev/v4l/by-path/{port_other_bus}-video-index0", 1
        )

        with caplog.at_level(logging.WARNING):
            result = assign_slots([other, a], slot_count=1, pins={0: "usb-0:1.3"})

        assert result == [a]
        assert any("ambiguous pin" in r.getMessage() for r in caplog.records)

    def test_pin_never_claims_longer_port_number_when_exact_port_absent(self):
        # SAFETY: slot0 pinned to "usb-0:1.3"; that camera is unplugged while
        # "usb-0:1.30" is present. The pin must NOT claim 1.30 -- the slot
        # stays honestly empty rather than surfacing the wrong camera.
        port_wide = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.30:1.0"
        wide = CameraIdentity(
            port_wide, f"/dev/v4l/by-path/{port_wide}-video-index0", 1
        )

        result = assign_slots([wide], slot_count=2, pins={0: "usb-0:1.3"})

        assert result[0] is None
        assert result[1] == wide

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
