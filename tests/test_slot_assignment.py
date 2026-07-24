"""Pure startup and reattach slot-assignment contracts.

``assign_slots`` handles a discovery batch, while ``choose_slot_for_identity``
places one returning camera. Both share the safety invariant that a reserved
tile stays empty rather than displaying a camera that does not match its pin.
Plain identities are sufficient here; Qt and OpenCV are intentionally absent.
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
    """Protect component-boundary matching for shortened USB port path pins.

    A substring check could let ``usb-0:1.1`` claim ``usb-0:1.10`` or a
    chained-hub descendant. Boundary checks keep a safety-relevant tile tied
    to the intended physical connector.
    """

    def test_port_tail_pin_matches_its_own_port(self):
        """A conventional USB suffix identifies its full USB port path."""
        assert _pin_matches("usb-0:1.3", PORT_A) is True

    def test_bare_fragment_pin_matches_at_boundaries(self):
        """The supported short numeric form matches a whole port component."""
        assert _pin_matches("1.4", PORT_B) is True

    def test_full_port_path_pin_matches_exactly(self):
        """Operators may pin with the complete USB port path."""
        assert _pin_matches(PORT_A, PORT_A) is True

    def test_pin_does_not_match_longer_port_number(self):
        """Port 1.1 does not alias the distinct multidigit port 1.10."""
        port_10 = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.10:1.0"
        assert _pin_matches("usb-0:1.1", port_10) is False

    def test_pin_does_not_match_chained_hub_subport(self):
        """A parent-port pin does not claim a camera behind a child hub."""
        port_chained = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.1.2:1.0"
        assert _pin_matches("usb-0:1.1", port_chained) is False

    def test_bare_fragment_pin_does_not_match_chained_hub_tail(self):
        """A short pin cannot begin inside another dotted port number."""
        # Here ``1.1`` is only the trailing portion of the camera's actual
        # ``2.1.1`` route, not the whole connector identifier.
        port_chained_tail = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:2.1.1:1.0"
        assert _pin_matches("1.1", port_chained_tail) is False

    def test_bare_fragment_pin_still_matches_whole_port_number(self):
        """The same short form remains valid at a real component boundary."""
        port_11 = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.1:1.0"
        assert _pin_matches("1.1", port_11) is True

    def test_pin_does_not_match_starting_mid_token(self):
        """Text found inside a named component is not a valid port tail."""
        assert _pin_matches("b-0:1.3", PORT_A) is False

    def test_index_pin_requires_exact_match(self):
        """Degraded ``index:N`` pins compare exactly, including digit boundaries."""
        assert _pin_matches("index:1", "index:1") is True
        assert _pin_matches("index:1", "index:10") is False


class TestAssignSlots:
    """Define deterministic bulk assignment at application startup."""

    def test_default_port_order_fills_slots_in_order(self):
        """Without pins, discovery order fills slots from the beginning."""
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)
        b = CameraIdentity(PORT_B, f"/dev/v4l/by-path/{PORT_B}-video-index0", 1)

        result = assign_slots([a, b], slot_count=3, pins={})

        assert result == [a, b, None]

    def test_fewer_cameras_than_slots_leaves_trailing_none(self):
        """Unused grid positions remain explicit ``None`` placeholders."""
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)

        result = assign_slots([a], slot_count=3, pins={})

        assert result == [a, None, None]

    def test_extra_cameras_are_dropped_and_logged(self, caplog):
        """Cameras beyond grid capacity are omitted with operator-visible context."""
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
        """A matching reservation takes precedence over discovery order."""
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)
        b = CameraIdentity(PORT_B, f"/dev/v4l/by-path/{PORT_B}-video-index0", 1)

        # Put the nonmatching camera first so the reservation is what changes slot 0.
        result = assign_slots([a, b], slot_count=2, pins={0: "1.4"})

        assert result[0] == b
        assert result[1] == a

    def test_pinned_port_absent_stays_none_even_with_spare_cameras(self):
        """An absent reserved camera leaves its tile empty."""
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)
        b = CameraIdentity(PORT_B, f"/dev/v4l/by-path/{PORT_B}-video-index0", 1)

        # Spare cameras may use other slots, but backfilling slot 0 would violate
        # the operator's physical-camera mapping.
        result = assign_slots([a, b], slot_count=3, pins={0: "1.99"})

        assert result[0] is None
        assert a in result
        assert b in result

    def test_ambiguous_pin_picks_natural_first_and_warns(self, caplog):
        """Ambiguous short pins resolve deterministically and emit a warning."""
        # The same USB port suffix can exist on multiple controllers. Reverse input
        # order to show that natural USB port path order breaks the tie.
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
        """An unplugged pinned camera cannot be impersonated by port 1.30."""
        port_wide = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.30:1.0"
        wide = CameraIdentity(
            port_wide, f"/dev/v4l/by-path/{port_wide}-video-index0", 1
        )

        result = assign_slots([wide], slot_count=2, pins={0: "usb-0:1.3"})

        assert result[0] is None
        assert result[1] == wide

    def test_index_pin_exact_matches_and_does_not_substring_match(self):
        """A numeric fallback pin selects index 1 without also claiming index 10."""
        ident1 = CameraIdentity("index:1", None, 1)
        ident10 = CameraIdentity("index:10", None, 10)

        result = assign_slots([ident1, ident10], slot_count=2, pins={0: "index:1"})

        assert result[0] == ident1
        assert ident10 in result
        assert result[1] == ident10

    def test_pin_claimed_identity_not_duplicated_in_unpinned_fill(self):
        """An identity consumed by a pin is removed from ordinary fill candidates."""
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)
        b = CameraIdentity(PORT_B, f"/dev/v4l/by-path/{PORT_B}-video-index0", 1)

        result = assign_slots([a, b], slot_count=3, pins={0: "1.3"})

        assert result[0] == a
        assert result.count(a) == 1
        assert b in result

    def test_returns_exactly_slot_count_entries(self):
        """The result always mirrors grid size, even with no discoveries."""
        result = assign_slots([], slot_count=3, pins={})
        assert len(result) == 3
        assert result == [None, None, None]


class TestChooseSlotForIdentity:
    """Define placement precedence for one camera discovered during rescan."""

    def test_prefers_matching_pinned_free_slot(self):
        """A free matching reservation is the highest-priority destination."""
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)

        slot = choose_slot_for_identity(
            a, free_slot_indexes=[0, 1, 2], pins={1: "1.3"}, last_slot_by_port={}
        )

        assert slot == 1

    def test_returns_to_last_slot_after_replug_index_independent(self):
        """USB port path memory survives numeric index changes after a replug."""
        # Udev may assign a new ``videoN`` while the USB port path remains
        # unchanged; slot memory must therefore key by ``port_path``.
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
        """A new reservation overrides stale slot memory for another camera."""
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)

        # Slot 2 remains free in the runtime sense but is no longer eligible for A.
        slot = choose_slot_for_identity(
            a,
            free_slot_indexes=[0, 1, 2],
            pins={2: "1.4"},
            last_slot_by_port={PORT_A: 2},
        )

        assert slot == 0

    def test_falls_back_to_lowest_free_unpinned_slot(self):
        """With no pin or memory match, placement uses the lowest eligible slot."""
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)

        slot = choose_slot_for_identity(
            a, free_slot_indexes=[1, 2], pins={}, last_slot_by_port={}
        )

        assert slot == 1

    def test_returns_none_when_only_non_matching_pinned_slots_are_free(self):
        """No eligible tile returns ``None`` so the camera can wait for rescan."""
        a = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)

        slot = choose_slot_for_identity(
            a,
            free_slot_indexes=[1, 2],
            pins={1: "1.4", 2: "1.10"},
            last_slot_by_port={},
        )

        assert slot is None
