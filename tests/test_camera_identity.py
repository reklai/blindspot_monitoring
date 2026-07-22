"""
Tests for core/camera.py camera identity model + /dev/v4l/by-path discovery.

Covers CameraIdentity, list_by_path_nodes, _test_identity, _natural_key,
discover_camera_identities and find_working_camera_identities, plus the
find_working_cameras() compatibility wrapper. No real devices are touched;
a fake /dev/v4l/by-path tree is built under tmp_path per test.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fake_by_path(tmp_path, monkeypatch):
    """Build a fake /dev/v4l/by-path tree.

    Layout: tmp_path/dev/videoN are real files standing in for capture
    nodes; tmp_path/dev/by-path/<name>-video-indexN are symlinks pointing
    at them, mirroring how the kernel actually populates by-path.

    Monkeypatches core.camera.BY_PATH_DIR to the fake by-path dir and
    resets the module's one-shot "degraded" warning flag so tests don't
    leak state into each other.
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

    return {
        "dev_dir": dev_dir,
        "by_path_dir": by_path_dir,
        "make_link": make_link,
    }


PORT_A = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0"
PORT_B = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.4:1.0"


class TestListByPathNodes:
    """Requirement 1: grouping and skip behavior."""

    def test_groups_two_ports_with_sorted_nodes(self, fake_by_path):
        by_path_dir = fake_by_path["by_path_dir"]
        make_link = fake_by_path["make_link"]

        make_link(f"{PORT_A}-video-index1", 1)
        make_link(f"{PORT_A}-video-index0", 0)
        make_link(f"{PORT_B}-video-index0", 2)
        make_link(f"{PORT_B}-video-index1", 3)
        # Non-matching entry name: must be skipped even though it resolves
        # to a real video node.
        (by_path_dir / "not-a-camera-entry").symlink_to(fake_by_path["dev_dir"] / "video0")

        from core.camera import list_by_path_nodes

        groups = list_by_path_nodes(str(by_path_dir))

        assert set(groups.keys()) == {PORT_A, PORT_B}
        assert [idx for idx, _ in groups[PORT_A]] == [0, 1]
        assert [idx for idx, _ in groups[PORT_B]] == [2, 3]

    def test_missing_dir_returns_empty_dict(self, tmp_path):
        from core.camera import list_by_path_nodes

        assert list_by_path_nodes(str(tmp_path / "does-not-exist")) == {}

    def test_dangling_symlink_is_skipped(self, fake_by_path):
        """A by-path entry whose realpath target doesn't exist (device was
        unplugged but the symlink hasn't been cleaned up yet) must be
        skipped rather than surfaced as a phantom camera group."""
        by_path_dir = fake_by_path["by_path_dir"]
        dev_dir = fake_by_path["dev_dir"]

        # Symlink to a video node that was never created (dangling target),
        # named so it still matches the "-video-indexN" pattern.
        (by_path_dir / f"{PORT_A}-video-index0").symlink_to(dev_dir / "video0")

        from core.camera import list_by_path_nodes

        groups = list_by_path_nodes(str(by_path_dir))

        assert groups == {}


class TestTestIdentity:
    """Requirements 2-3: metadata-node dedupe and lowest-index-wins."""

    def test_dedupes_metadata_node_when_capture_node_is_not_index0(self, fake_by_path):
        by_path_dir = fake_by_path["by_path_dir"]
        make_link = fake_by_path["make_link"]
        make_link(f"{PORT_A}-video-index0", 0)  # metadata node, fails grab()
        make_link(f"{PORT_A}-video-index1", 1)  # actual capture node

        from core.camera import list_by_path_nodes, _test_identity

        groups = list_by_path_nodes(str(by_path_dir))

        with patch("core.camera.test_single_camera") as mock_test:
            mock_test.side_effect = lambda idx, **kw: idx if idx == 1 else None
            ident = _test_identity(PORT_A, groups[PORT_A], str(by_path_dir))

        assert ident is not None
        assert ident.port_path == PORT_A
        assert ident.index == 1
        assert ident.device_path == str(by_path_dir / f"{PORT_A}-video-index1")

    def test_lowest_node_wins_when_both_grab(self, fake_by_path):
        by_path_dir = fake_by_path["by_path_dir"]
        make_link = fake_by_path["make_link"]
        make_link(f"{PORT_A}-video-index0", 0)
        make_link(f"{PORT_A}-video-index1", 1)

        from core.camera import list_by_path_nodes, _test_identity

        groups = list_by_path_nodes(str(by_path_dir))

        with patch("core.camera.test_single_camera") as mock_test:
            mock_test.side_effect = lambda idx, **kw: idx
            ident = _test_identity(PORT_A, groups[PORT_A], str(by_path_dir))

        assert ident.index == 0
        assert ident.device_path == str(by_path_dir / f"{PORT_A}-video-index0")
        assert mock_test.call_count == 1

    def test_all_nodes_fail_returns_none(self, fake_by_path):
        by_path_dir = fake_by_path["by_path_dir"]
        make_link = fake_by_path["make_link"]
        make_link(f"{PORT_A}-video-index0", 0)

        from core.camera import list_by_path_nodes, _test_identity

        groups = list_by_path_nodes(str(by_path_dir))

        with patch("core.camera.test_single_camera", return_value=None):
            ident = _test_identity(PORT_A, groups[PORT_A], str(by_path_dir))

        assert ident is None


class TestNaturalKey:
    """Requirement 3: natural sort ('1.2' before '1.10')."""

    def test_natural_key_orders_multidigit_segments_numerically(self):
        from core.camera import _natural_key

        ports = ["usb-0:1.10", "usb-0:1.2"]
        assert sorted(ports, key=_natural_key) == ["usb-0:1.2", "usb-0:1.10"]


class TestDiscoverCameraIdentities:
    """Requirement 6: cheap, non-probing discovery."""

    def test_natural_sort_of_by_path_identities(self, fake_by_path, monkeypatch):
        by_path_dir = fake_by_path["by_path_dir"]
        make_link = fake_by_path["make_link"]

        port_big = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.10:1.0"
        port_small = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2:1.0"
        make_link(f"{port_big}-video-index0", 0)
        make_link(f"{port_small}-video-index0", 1)

        monkeypatch.setattr("core.camera.get_video_indexes", lambda: [0, 1])

        from core.camera import discover_camera_identities

        idents = discover_camera_identities(str(by_path_dir))

        assert [i.port_path for i in idents] == [port_small, port_big]

    def test_missing_by_path_dir_falls_back_to_numeric_order(self, tmp_path, monkeypatch, caplog):
        missing_dir = str(tmp_path / "nonexistent-by-path")
        monkeypatch.setattr("core.camera.BY_PATH_DIR", missing_dir)
        monkeypatch.setattr("core.camera._by_path_degraded_warned", False)
        monkeypatch.setattr("core.camera.get_video_indexes", lambda: [2, 0, 1])

        from core.camera import discover_camera_identities

        with caplog.at_level(logging.WARNING):
            first = discover_camera_identities()
            second = discover_camera_identities()

        assert [i.index for i in first] == [0, 1, 2]
        assert [i.port_path for i in first] == ["index:0", "index:1", "index:2"]
        assert all(i.device_path is None for i in first)
        assert first == second

        degraded_warnings = [
            r for r in caplog.records
            if "degraded to enumeration order" in r.getMessage()
        ]
        assert len(degraded_warnings) == 1

    def test_mixed_tree_by_path_identity_before_orphan(self, fake_by_path, monkeypatch):
        by_path_dir = fake_by_path["by_path_dir"]
        make_link = fake_by_path["make_link"]
        make_link(f"{PORT_A}-video-index0", 0)
        # index 5 has no by-path entry at all -> orphan fallback.
        monkeypatch.setattr("core.camera.get_video_indexes", lambda: [0, 5])

        from core.camera import discover_camera_identities

        idents = discover_camera_identities(str(by_path_dir))

        assert len(idents) == 2
        assert idents[0].port_path == PORT_A
        assert idents[0].device_path is not None
        assert idents[1].port_path == "index:5"
        assert idents[1].device_path is None
        assert idents[1].index == 5


class TestFindWorkingCameraIdentities:
    """Requirement 7: probing discovery, one submission per physical group."""

    def test_end_to_end_over_fake_tree(self, fake_by_path, monkeypatch, caplog):
        by_path_dir = fake_by_path["by_path_dir"]
        make_link = fake_by_path["make_link"]

        port_low = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2:1.0"
        port_high = "platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.10:1.0"

        make_link(f"{port_low}-video-index0", 0)  # metadata node
        make_link(f"{port_low}-video-index1", 1)  # capture node
        make_link(f"{port_high}-video-index0", 2)  # capture node

        monkeypatch.setattr("core.camera.get_video_indexes", lambda: [0, 1, 2])

        from core.camera import CameraIdentity, find_working_camera_identities

        identity_low = CameraIdentity(
            port_low, str(by_path_dir / f"{port_low}-video-index1"), 1
        )
        identity_high = CameraIdentity(
            port_high, str(by_path_dir / f"{port_high}-video-index0"), 2
        )

        test_identity_calls = []

        def fake_test_identity(port_path, node_indexes, directory, **kw):
            test_identity_calls.append(port_path)
            if port_path == port_low:
                return identity_low
            if port_path == port_high:
                return identity_high
            return None

        mock_confirm = MagicMock(side_effect=lambda idx, **kw: idx)

        with patch("core.camera._test_identity", side_effect=fake_test_identity), \
             patch("core.camera.test_single_camera", mock_confirm):
            with caplog.at_level(logging.INFO):
                idents = find_working_camera_identities(str(by_path_dir))

        # One _test_identity submission per physical group (2 groups), not per node.
        assert sorted(test_identity_calls) == sorted([port_low, port_high])
        assert len(test_identity_calls) == 2

        # Round 2 re-confirms each surviving identity's resolved index.
        assert mock_confirm.call_count == 2
        confirmed_indexes = {c.args[0] for c in mock_confirm.call_args_list}
        assert confirmed_indexes == {1, 2}

        assert [i.port_path for i in idents] == [port_low, port_high]
        assert idents[0].index == 1
        assert idents[1].index == 2

        assert any("FINAL Working camera identities" in r.getMessage() for r in caplog.records)

    def test_round2_failure_drops_identity(self, fake_by_path, monkeypatch):
        by_path_dir = fake_by_path["by_path_dir"]
        make_link = fake_by_path["make_link"]
        make_link(f"{PORT_A}-video-index0", 0)
        monkeypatch.setattr("core.camera.get_video_indexes", lambda: [0])

        from core.camera import find_working_camera_identities

        with patch("core.camera.test_single_camera") as mock_test:
            # Round 1 succeeds, round 2 (allow_kill=False) fails.
            def side_effect(idx, **kw):
                if kw.get("allow_kill", True) is False:
                    return None
                return idx

            mock_test.side_effect = side_effect
            idents = find_working_camera_identities(str(by_path_dir))

        assert idents == []


class TestFindWorkingCamerasWrapper:
    """Requirement 8: compatibility wrapper delegates to identities."""

    def test_wrapper_returns_identity_indexes(self, monkeypatch):
        import core.camera as camera_module
        from core.camera import CameraIdentity, find_working_cameras

        fake_idents = [
            CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0),
            CameraIdentity("index:2", None, 2),
        ]
        monkeypatch.setattr(
            camera_module, "find_working_camera_identities", lambda: fake_idents
        )

        assert find_working_cameras() == [0, 2]


class TestStreamTarget:
    """Requirement 2 (property): stream_target picks device_path or index."""

    def test_by_path_identity_stream_target_is_device_path(self):
        from core.camera import CameraIdentity

        ident = CameraIdentity(PORT_A, f"/dev/v4l/by-path/{PORT_A}-video-index0", 0)
        assert ident.stream_target == f"/dev/v4l/by-path/{PORT_A}-video-index0"

    def test_fallback_identity_stream_target_is_index(self):
        from core.camera import CameraIdentity

        ident = CameraIdentity("index:3", None, 3)
        assert ident.stream_target == 3
