"""
Tests for core/config.py - Configuration parsing and validation.
"""

import configparser
import logging
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Import after path setup in conftest.py
from core import config


class TestConfigHelpers:
    """Test helper functions for config parsing."""

    def test_as_bool_true_values(self):
        """Test _as_bool recognizes true values."""
        for val in ("true", "True", "TRUE", "yes", "Yes", "1", "on", "On"):
            assert config._as_bool(val, False) is True

    def test_as_bool_false_values(self):
        """Test _as_bool recognizes false values."""
        for val in ("false", "False", "FALSE", "no", "No", "0", "off", "Off"):
            assert config._as_bool(val, True) is False

    def test_as_bool_default(self):
        """Test _as_bool returns default for invalid values."""
        assert config._as_bool("invalid", True) is True
        assert config._as_bool("invalid", False) is False
        assert config._as_bool("", True) is True

    def test_as_int_valid(self):
        """Test _as_int parses valid integers."""
        assert config._as_int("42", 0) == 42
        assert config._as_int("-10", 0) == -10
        assert config._as_int("0", 99) == 0

    def test_as_int_with_bounds(self):
        """Test _as_int respects min/max bounds."""
        assert config._as_int("100", 0, min_value=0, max_value=50) == 50
        assert config._as_int("-10", 0, min_value=0, max_value=50) == 0
        assert config._as_int("25", 0, min_value=0, max_value=50) == 25

    def test_as_int_default(self):
        """Test _as_int returns default for invalid values."""
        assert config._as_int("not_a_number", 42) == 42
        assert config._as_int("", 99) == 99
        assert config._as_int("3.14", 0) == 0  # Floats are invalid

    def test_as_float_valid(self):
        """Test _as_float parses valid floats."""
        assert config._as_float("3.14", 0.0) == pytest.approx(3.14)
        assert config._as_float("-2.5", 0.0) == pytest.approx(-2.5)
        assert config._as_float("42", 0.0) == pytest.approx(42.0)

    def test_as_float_with_bounds(self):
        """Test _as_float respects min/max bounds."""
        assert config._as_float("1.5", 0.0, min_value=0.0, max_value=1.0) == pytest.approx(1.0)
        assert config._as_float("-0.5", 0.0, min_value=0.0, max_value=1.0) == pytest.approx(0.0)
        assert config._as_float("0.75", 0.0, min_value=0.0, max_value=1.0) == pytest.approx(0.75)

    def test_as_float_default(self):
        """Test _as_float returns default for invalid values."""
        assert config._as_float("not_a_number", 3.14) == pytest.approx(3.14)
        assert config._as_float("", 2.5) == pytest.approx(2.5)


class TestLoadConfig:
    """Test config file loading."""

    def test_load_config_default_path(self):
        """Test loading config from default path."""
        parser = config.load_config()
        assert isinstance(parser, configparser.ConfigParser)

    def test_load_config_custom_path(self, temp_config_file):
        """Test loading config from custom path."""
        parser = config.load_config(str(temp_config_file))
        assert isinstance(parser, configparser.ConfigParser)
        assert parser.has_section("logging")
        assert parser.has_section("performance")
        assert parser.has_section("camera")

    def test_load_config_missing_file(self, tmp_path):
        """Test loading non-existent config returns empty parser."""
        missing_path = tmp_path / "nonexistent.ini"
        parser = config.load_config(str(missing_path))
        assert isinstance(parser, configparser.ConfigParser)

    def test_load_config_env_override(self, temp_config_file):
        """Test CAMERA_DASHBOARD_CONFIG env var overrides default path."""
        with patch.dict(os.environ, {"CAMERA_DASHBOARD_CONFIG": str(temp_config_file)}):
            parser = config.load_config()
            assert parser.has_section("logging")


class TestApplyConfig:
    """Test config application to global variables."""

    def test_apply_config_sets_globals(self, temp_config_file, save_restore_config):
        """Test apply_config sets module-level variables."""
        parser = config.load_config(str(temp_config_file))
        config.apply_config(parser)

        # Check some key values were set
        assert config.CAMERA_SLOT_COUNT == 3
        assert config.PROFILE_CAPTURE_WIDTH == 640
        assert config.PROFILE_CAPTURE_HEIGHT == 480
        assert config.PROFILE_CAPTURE_FPS == 20
        assert config.PROFILE_UI_FPS == 15

    def test_apply_config_bounds_checking(self, tmp_path, save_restore_config):
        """Test apply_config enforces bounds on values."""
        config_file = tmp_path / "test.ini"
        config_file.write_text("""
[camera]
slot_count = 100

[performance]
cpu_load_threshold = 5.0
""")
        parser = config.load_config(str(config_file))
        config.apply_config(parser)

        # slot_count should be clamped to max 8
        assert config.CAMERA_SLOT_COUNT <= 8
        # cpu_load_threshold should be clamped to max 1.0
        assert config.CPU_LOAD_THRESHOLD <= 1.0


class TestChooseProfile:
    """Test profile selection."""

    def test_choose_profile_returns_tuple(self):
        """Test choose_profile returns (width, height, fps, ui_fps)."""
        result = config.choose_profile()
        assert isinstance(result, tuple)
        assert len(result) == 4


class TestFirstFrameTimeoutConfig:
    """Test parsing of [performance] first_frame_timeout_sec."""

    def test_default_value(self):
        """Default is 10.0 seconds, field-tunable like stale_frame_timeout_sec."""
        assert config.FIRST_FRAME_TIMEOUT_SEC == 10.0

    def test_parses_configured_value(self, tmp_path, save_restore_config):
        """A configured value overrides the default."""
        config_file = tmp_path / "test.ini"
        config_file.write_text(
            "[performance]\nfirst_frame_timeout_sec = 20.0\n"
        )
        parser = config.load_config(str(config_file))
        config.apply_config(parser)

        assert config.FIRST_FRAME_TIMEOUT_SEC == 20.0

    def test_enforces_minimum_bound(self, tmp_path, save_restore_config):
        """Values below the 2.0s minimum are clamped."""
        config_file = tmp_path / "test.ini"
        config_file.write_text(
            "[performance]\nfirst_frame_timeout_sec = 0.1\n"
        )
        parser = config.load_config(str(config_file))
        config.apply_config(parser)

        assert config.FIRST_FRAME_TIMEOUT_SEC >= 2.0

    def test_choose_profile_values(self, save_restore_config):
        """Test choose_profile returns configured values unchanged, consistently."""
        # Set known values
        config.PROFILE_CAPTURE_WIDTH = 640
        config.PROFILE_CAPTURE_HEIGHT = 480
        config.PROFILE_CAPTURE_FPS = 20
        config.PROFILE_UI_FPS = 15
        config.MIN_DYNAMIC_FPS = 5
        config.MIN_DYNAMIC_UI_FPS = 10

        # Values pass through from config unchanged, regardless of how many
        # times it's called (no camera-count scaling).
        for _ in range(3):
            w, h, fps, ui_fps = config.choose_profile()
            assert w == 640
            assert h == 480
            assert fps == 20
            assert ui_fps == 15


class TestSlotPins:
    """Test [slots] pinning section parsing into config.SLOT_PINS."""

    def test_valid_pins_parsed(self, tmp_path, save_restore_config):
        """Valid slotN keys are parsed into an int-keyed dict."""
        config_file = tmp_path / "test.ini"
        config_file.write_text(
            """
[camera]
slot_count = 3

[slots]
slot0 = usb-0:1.1
slot1 = usb-0:1.2
"""
        )
        parser = config.load_config(str(config_file))
        config.apply_config(parser)

        assert config.SLOT_PINS == {0: "usb-0:1.1", 1: "usb-0:1.2"}

    def test_absent_section_yields_empty_dict(self, tmp_path, save_restore_config):
        """No [slots] section at all -> SLOT_PINS is {}."""
        config_file = tmp_path / "test.ini"
        config_file.write_text(
            """
[camera]
slot_count = 3
"""
        )
        parser = config.load_config(str(config_file))
        config.apply_config(parser)

        assert config.SLOT_PINS == {}

    def test_invalid_key_format_skipped_with_warning(self, tmp_path, save_restore_config, caplog):
        """A key that doesn't fullmatch 'slot(\\d+)' is skipped and warned."""
        config_file = tmp_path / "test.ini"
        config_file.write_text(
            """
[camera]
slot_count = 3

[slots]
slotx = usb-0:1.1
"""
        )
        parser = config.load_config(str(config_file))
        with caplog.at_level(logging.WARNING):
            config.apply_config(parser)

        assert config.SLOT_PINS == {}
        assert any("slotx" in r.getMessage() for r in caplog.records)

    def test_out_of_range_index_skipped_with_warning(self, tmp_path, save_restore_config, caplog):
        """A slot index beyond CAMERA_SLOT_COUNT - 1 is skipped and warned."""
        config_file = tmp_path / "test.ini"
        config_file.write_text(
            """
[camera]
slot_count = 3

[slots]
slot99 = usb-0:1.1
"""
        )
        parser = config.load_config(str(config_file))
        with caplog.at_level(logging.WARNING):
            config.apply_config(parser)

        assert config.SLOT_PINS == {}
        assert any("slot99" in r.getMessage() for r in caplog.records)

    def test_empty_value_skipped_with_warning(self, tmp_path, save_restore_config, caplog):
        """An empty (whitespace-only) value is skipped and warned."""
        config_file = tmp_path / "test.ini"
        config_file.write_text(
            """
[camera]
slot_count = 3

[slots]
slot0 =
"""
        )
        parser = config.load_config(str(config_file))
        with caplog.at_level(logging.WARNING):
            config.apply_config(parser)

        assert config.SLOT_PINS == {}
        assert any("slot0" in r.getMessage() for r in caplog.records)

    def test_duplicate_pin_value_both_kept_with_warning(
        self, tmp_path, save_restore_config, caplog
    ):
        """Two different slots pinned to the identical value are both
        kept in SLOT_PINS (matching is resolved at assignment time), but
        a warning is logged since only one can actually match a
        camera."""
        config_file = tmp_path / "test.ini"
        config_file.write_text(
            """
[camera]
slot_count = 3

[slots]
slot0 = usb-0:1.1
slot1 = usb-0:1.1
"""
        )
        parser = config.load_config(str(config_file))
        with caplog.at_level(logging.WARNING):
            config.apply_config(parser)

        assert config.SLOT_PINS == {0: "usb-0:1.1", 1: "usb-0:1.1"}
        assert any(
            "slot0" in r.getMessage()
            and "slot1" in r.getMessage()
            and "usb-0:1.1" in r.getMessage()
            for r in caplog.records
        )

    def test_repeated_apply_config_produces_fresh_dict(self, tmp_path, save_restore_config):
        """apply_config never mutates the previous SLOT_PINS dict in place."""
        config_file = tmp_path / "test.ini"
        config_file.write_text(
            """
[camera]
slot_count = 3

[slots]
slot0 = usb-0:1.1
"""
        )
        parser = config.load_config(str(config_file))
        config.apply_config(parser)

        config.SLOT_PINS["slot1"] = "mutated-in-place"  # mutate the returned dict
        config.apply_config(parser)

        assert config.SLOT_PINS == {0: "usb-0:1.1"}


class TestConfigDefaults:
    """Test that config has sensible defaults."""

    def test_default_camera_slot_count(self):
        """Test default camera slot count is reasonable."""
        assert 1 <= config.CAMERA_SLOT_COUNT <= 8

    def test_default_cpu_thresholds(self):
        """Test default CPU thresholds are reasonable."""
        assert 0.0 < config.CPU_LOAD_THRESHOLD <= 1.0
        assert 50.0 < config.CPU_TEMP_THRESHOLD_C < 100.0

    def test_default_fps_values(self):
        """Test default FPS values are reasonable."""
        assert 1 <= config.MIN_DYNAMIC_FPS <= 30
        assert 1 <= config.MIN_DYNAMIC_UI_FPS <= 30
        assert config.MIN_DYNAMIC_FPS <= config.PROFILE_CAPTURE_FPS
