"""Configuration parsing contracts and process-global application behavior.

These tests distinguish raw conversion rules from ``apply_config`` side
effects. Tests that write globals opt into ``save_restore_config`` so a new
developer can reason about each case without hidden ordering dependencies.
"""

import configparser
import logging
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# ``conftest`` first adds the repository root, matching the application's
# top-level ``core`` import layout.
from core import config


class TestConfigHelpers:
    """Document the tolerant scalar conversions used for INI values."""

    def test_as_bool_true_values(self):
        """Common truthy spellings are case-insensitive."""
        for val in ("true", "True", "TRUE", "yes", "Yes", "1", "on", "On"):
            assert config._as_bool(val, False) is True

    def test_as_bool_false_values(self):
        """Common false spellings are case-insensitive."""
        for val in ("false", "False", "FALSE", "no", "No", "0", "off", "Off"):
            assert config._as_bool(val, True) is False

    def test_as_bool_default(self):
        """Unknown or empty text preserves the caller-supplied boolean default."""
        assert config._as_bool("invalid", True) is True
        assert config._as_bool("invalid", False) is False
        assert config._as_bool("", True) is True

    def test_as_int_valid(self):
        """Signed and zero integer text is accepted without changing its value."""
        assert config._as_int("42", 0) == 42
        assert config._as_int("-10", 0) == -10
        assert config._as_int("0", 99) == 0

    def test_as_int_with_bounds(self):
        """Out-of-range integers clamp at either configured boundary."""
        assert config._as_int("100", 0, min_value=0, max_value=50) == 50
        assert config._as_int("-10", 0, min_value=0, max_value=50) == 0
        assert config._as_int("25", 0, min_value=0, max_value=50) == 25

    def test_as_int_default(self):
        """Malformed integer text falls back instead of failing configuration load."""
        assert config._as_int("not_a_number", 42) == 42
        assert config._as_int("", 99) == 99
        # Decimal text is intentionally invalid for integer-only settings.
        assert config._as_int("3.14", 0) == 0

    def test_as_float_valid(self):
        """Float settings accept decimal, signed, and integer-shaped text."""
        assert config._as_float("3.14", 0.0) == pytest.approx(3.14)
        assert config._as_float("-2.5", 0.0) == pytest.approx(-2.5)
        assert config._as_float("42", 0.0) == pytest.approx(42.0)

    def test_as_float_with_bounds(self):
        """Out-of-range floats clamp while an interior value passes through."""
        assert config._as_float("1.5", 0.0, min_value=0.0, max_value=1.0) == pytest.approx(1.0)
        assert config._as_float("-0.5", 0.0, min_value=0.0, max_value=1.0) == pytest.approx(0.0)
        assert config._as_float("0.75", 0.0, min_value=0.0, max_value=1.0) == pytest.approx(0.75)

    def test_as_float_default(self):
        """Malformed float text leaves the setting at its supplied default."""
        assert config._as_float("not_a_number", 3.14) == pytest.approx(3.14)
        assert config._as_float("", 2.5) == pytest.approx(2.5)


class TestLoadConfig:
    """Config path selection stays independent of a developer's local file."""

    def test_load_config_default_path(self):
        """Default-path loading always returns a parser, even if the file is absent."""
        parser = config.load_config()
        assert isinstance(parser, configparser.ConfigParser)

    def test_load_config_custom_path(self, temp_config_file):
        """An explicit path loads the representative fixture's application sections."""
        parser = config.load_config(str(temp_config_file))
        assert isinstance(parser, configparser.ConfigParser)
        assert parser.has_section("logging")
        assert parser.has_section("performance")
        assert parser.has_section("camera")

    def test_load_config_missing_file(self, tmp_path):
        """A missing explicit file degrades to an empty parser rather than raising."""
        missing_path = tmp_path / "nonexistent.ini"
        parser = config.load_config(str(missing_path))
        assert isinstance(parser, configparser.ConfigParser)

    def test_load_config_env_override(self, temp_config_file):
        """The environment override is honored when no explicit path is passed."""
        with patch.dict(os.environ, {"CAMERA_DASHBOARD_CONFIG": str(temp_config_file)}):
            parser = config.load_config()
            assert parser.has_section("logging")


class TestApplyConfig:
    """Parsed section values populate their runtime module globals."""

    def test_apply_config_sets_globals(self, temp_config_file, save_restore_config):
        """Representative profile and camera values reach their runtime globals."""
        parser = config.load_config(str(temp_config_file))
        config.apply_config(parser)

        # Sample both camera layout and capture profile fields to cross section
        # boundaries without duplicating every parser assertion.
        assert config.CAMERA_SLOT_COUNT == 3
        assert config.PROFILE_CAPTURE_WIDTH == 640
        assert config.PROFILE_CAPTURE_HEIGHT == 480
        assert config.PROFILE_CAPTURE_FPS == 20
        assert config.PROFILE_UI_FPS == 15

    def test_apply_config_bounds_checking(self, tmp_path, save_restore_config):
        """Unsafe high values are capped at the application's supported limits."""
        config_file = tmp_path / "test.ini"
        # Both inputs deliberately exceed their documented range, selecting the
        # upper-clamp path rather than ordinary parsing.
        config_file.write_text("""
[camera]
slot_count = 100

[performance]
cpu_load_threshold = 5.0
""")
        parser = config.load_config(str(config_file))
        config.apply_config(parser)

        assert config.CAMERA_SLOT_COUNT <= 8
        assert config.CPU_LOAD_THRESHOLD <= 1.0


class TestChooseProfile:
    """Pin the profile tuple consumed by worker and widget construction."""

    def test_choose_profile_returns_tuple(self):
        """Callers receive the four values in width/height/capture/UI order."""
        result = config.choose_profile()
        assert isinstance(result, tuple)
        assert len(result) == 4

    def test_choose_profile_values(self, save_restore_config):
        """Repeated selection returns the configured profile without hidden state."""
        # Use distinct, valid values so tuple position and accidental clamping are
        # both visible.
        config.PROFILE_CAPTURE_WIDTH = 640
        config.PROFILE_CAPTURE_HEIGHT = 480
        config.PROFILE_CAPTURE_FPS = 20
        config.PROFILE_UI_FPS = 15
        config.MIN_DYNAMIC_FPS = 5
        config.MIN_DYNAMIC_UI_FPS = 10

        # Multiple calls expose any accidental call-count or camera-count adjustment.
        for _ in range(3):
            w, h, fps, ui_fps = config.choose_profile()
            assert w == 640
            assert h == 480
            assert fps == 20
            assert ui_fps == 15


class TestFirstFrameTimeoutConfig:
    """Define defaults and safety bounds for the first-frame watchdog."""

    def test_default_value(self):
        """Ten seconds provides startup grace when the field is omitted."""
        assert config.FIRST_FRAME_TIMEOUT_SEC == 10.0

    def test_parses_configured_value(self, tmp_path, save_restore_config):
        """A valid performance-section override reaches the runtime timeout."""
        config_file = tmp_path / "test.ini"
        config_file.write_text(
            "[performance]\nfirst_frame_timeout_sec = 20.0\n"
        )
        parser = config.load_config(str(config_file))
        config.apply_config(parser)

        assert config.FIRST_FRAME_TIMEOUT_SEC == 20.0

    def test_enforces_minimum_bound(self, tmp_path, save_restore_config):
        """Too-small values clamp to avoid restart loops during normal startup."""
        config_file = tmp_path / "test.ini"
        config_file.write_text(
            "[performance]\nfirst_frame_timeout_sec = 0.1\n"
        )
        parser = config.load_config(str(config_file))
        config.apply_config(parser)

        assert config.FIRST_FRAME_TIMEOUT_SEC >= 2.0


class TestSlotPins:
    """Optional USB port path reservations become ``SLOT_PINS``."""

    def test_valid_pins_parsed(self, tmp_path, save_restore_config):
        """Valid ``slotN`` keys map integer indexes to USB port path matches."""
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
        """Omitting the optional section clears reservations rather than retaining state."""
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
        """Malformed keys are ignored and named in a warning for operators."""
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
        """A reservation outside the configured grid is ignored with diagnostics."""
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
        """Whitespace-only reservations cannot silently reserve a slot."""
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
        """Duplicate values remain explicit but warn about the ambiguous setup.

        Assignment resolves the collision later, so parsing must not silently
        discard either configured slot even though one camera cannot fill both.
        """
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
        """Each apply rebuilds pin state instead of trusting a mutated old mapping."""
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

        # Simulate unrelated runtime code retaining and modifying the prior mapping.
        config.SLOT_PINS["slot1"] = "mutated-in-place"
        config.apply_config(parser)

        assert config.SLOT_PINS == {0: "usb-0:1.1"}


class TestConfigDefaults:
    """Keep built-in defaults inside ranges the UI and workers can operate."""

    def test_default_camera_slot_count(self):
        """The default grid is nonempty and does not exceed layout capacity."""
        assert 1 <= config.CAMERA_SLOT_COUNT <= 8

    def test_default_cpu_thresholds(self):
        """Default stress thresholds use normalized load and plausible temperature."""
        assert 0.0 < config.CPU_LOAD_THRESHOLD <= 1.0
        assert 50.0 < config.CPU_TEMP_THRESHOLD_C < 100.0

    def test_default_fps_values(self):
        """Dynamic-FPS floors are positive and compatible with the base profile."""
        assert 1 <= config.MIN_DYNAMIC_FPS <= 30
        assert 1 <= config.MIN_DYNAMIC_UI_FPS <= 30
        assert config.MIN_DYNAMIC_FPS <= config.PROFILE_CAPTURE_FPS
