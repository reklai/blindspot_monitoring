"""
Tests for core/config.py - Configuration parsing and validation.
"""

import configparser
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
    """Test profile selection based on camera count."""

    def test_choose_profile_returns_tuple(self):
        """Test choose_profile returns (width, height, fps, ui_fps)."""
        result = config.choose_profile(1)
        assert isinstance(result, tuple)
        assert len(result) == 4

    def test_choose_profile_values(self, save_restore_config):
        """Test choose_profile returns configured values without camera-count scaling."""
        # Set known values
        config.PROFILE_CAPTURE_WIDTH = 640
        config.PROFILE_CAPTURE_HEIGHT = 480
        config.PROFILE_CAPTURE_FPS = 20
        config.PROFILE_UI_FPS = 15
        config.MIN_DYNAMIC_FPS = 5
        config.MIN_DYNAMIC_UI_FPS = 10

        # Test with 1 camera (no scaling)
        w, h, fps, ui_fps = config.choose_profile(1)
        assert w == 640
        assert h == 480
        assert fps == 20
        assert ui_fps == 15
        
        # Test with 3 cameras (same configured values; runtime dynamic FPS handles load)
        w, h, fps, ui_fps = config.choose_profile(3)
        assert w == 640
        assert h == 480
        assert fps == 20
        assert ui_fps == 15
        
        # Test with 6 cameras (same configured values; runtime dynamic FPS handles load)
        w, h, fps, ui_fps = config.choose_profile(6)
        assert w == 640
        assert h == 480
        assert fps == 20
        assert ui_fps == 15


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


class TestOptionTable:
    """The INI schema table must stay in sync with the module globals."""

    def test_every_option_maps_to_an_existing_global(self):
        for option in config._OPTIONS:
            assert hasattr(config, option.name), option.name

    def test_option_keys_are_unique(self):
        keys = [(o.section, o.key) for o in config._OPTIONS]
        assert len(keys) == len(set(keys))
        names = [o.name for o in config._OPTIONS]
        assert len(names) == len(set(names))

    def test_apply_config_reads_every_section(self, temp_config_file, save_restore_config):
        config.apply_config(config.load_config(str(temp_config_file)))
        assert config.LOG_LEVEL == "INFO"
        assert config.LOG_FILE == "./logs/test.log" or config.LOG_FILE_ENV
        assert config.LOG_MAX_BYTES == 1048576
        assert config.LOG_BACKUP_COUNT == 2
        assert config.LOG_TO_STDOUT is False
        assert config.DYNAMIC_FPS_ENABLED is True
        assert config.MIN_DYNAMIC_FPS == 5
        assert config.CPU_TEMP_THRESHOLD_C == pytest.approx(75.0)
        assert config.KILL_DEVICE_HOLDERS is False
        assert config.USE_GSTREAMER is True
        assert config.HEALTH_LOG_INTERVAL_SEC == pytest.approx(30.0)

    def test_missing_keys_keep_current_values(self, tmp_path, save_restore_config):
        config.CAMERA_SLOT_COUNT = 5
        config.PROFILE_UI_FPS = 17
        cfg = tmp_path / "partial.ini"
        cfg.write_text("[camera]\nuse_gstreamer = false\n")
        config.apply_config(config.load_config(str(cfg)))
        assert config.USE_GSTREAMER is False
        assert config.CAMERA_SLOT_COUNT == 5
        assert config.PROFILE_UI_FPS == 17

    def test_unparsable_values_keep_current_values(self, tmp_path, save_restore_config):
        cfg = tmp_path / "bad.ini"
        cfg.write_text("[profile]\ncapture_fps = fast\n[logging]\nstdout = maybe\n")
        before_fps = config.PROFILE_CAPTURE_FPS
        before_stdout = config.LOG_TO_STDOUT
        config.apply_config(config.load_config(str(cfg)))
        assert config.PROFILE_CAPTURE_FPS == before_fps
        assert config.LOG_TO_STDOUT == before_stdout

    def test_lower_bounds_are_enforced(self, tmp_path, save_restore_config):
        cfg = tmp_path / "low.ini"
        cfg.write_text(
            "[performance]\nperf_check_interval_ms = 1\nstale_frame_timeout_sec = 0\n"
            "[camera]\nrescan_interval_ms = 10\n[profile]\ncapture_width = 8\n"
        )
        config.apply_config(config.load_config(str(cfg)))
        assert config.PERF_CHECK_INTERVAL_MS == 250
        assert config.STALE_FRAME_TIMEOUT_SEC == pytest.approx(0.5)
        assert config.RESCAN_INTERVAL_MS == 500
        assert config.PROFILE_CAPTURE_WIDTH == 160

    def test_log_file_env_override_wins(self, tmp_path, save_restore_config):
        cfg = tmp_path / "log.ini"
        cfg.write_text("[logging]\nfile = /from/ini.log\n")
        with patch.object(config, "LOG_FILE_ENV", "/from/env.log"):
            config.apply_config(config.load_config(str(cfg)))
        assert config.LOG_FILE == "/from/env.log"
