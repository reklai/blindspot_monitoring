"""
Configuration management for Camera Dashboard.

Handles loading config from INI files, environment variables,
and provides default values for all settings.
"""

from __future__ import annotations

import configparser
import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, NamedTuple, Optional


# ============================================================
# DEBUG FLAGS
# ============================================================
UI_FPS_LOGGING = False


# ============================================================
# LOGGING DEFAULTS
# ============================================================
LOG_LEVEL = "INFO"
LOG_FILE = "./logs/camera_dashboard.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
LOG_TO_STDOUT = True

CONFIG_PATH = os.environ.get("CAMERA_DASHBOARD_CONFIG", "./config.ini")
LOG_FILE_ENV = os.environ.get("CAMERA_DASHBOARD_LOG_FILE")


# ============================================================
# PERFORMANCE + RECOVERY TUNING
# ============================================================
DYNAMIC_FPS_ENABLED = True
PERF_CHECK_INTERVAL_MS = 2000
MIN_DYNAMIC_FPS = 10
MIN_DYNAMIC_UI_FPS = 12
UI_FPS_STEP = 2
CPU_LOAD_THRESHOLD = 0.75
CPU_TEMP_THRESHOLD_C = 75.0
STRESS_HOLD_COUNT = 3
RECOVER_HOLD_COUNT = 3

# Stale frame detection + bounded auto-restart policy.
STALE_FRAME_TIMEOUT_SEC = 1.5
RESTART_COOLDOWN_SEC = 5.0
MAX_RESTARTS_PER_WINDOW = 3
RESTART_WINDOW_SEC = 30.0


# ============================================================
# CAMERA RESCAN (HOT-PLUG SUPPORT)
# ============================================================
RESCAN_INTERVAL_MS = 15000
FAILED_CAMERA_COOLDOWN_SEC = 30.0


# ============================================================
# APP SETTINGS
# ============================================================
CAMERA_SLOT_COUNT = 3
HEALTH_LOG_INTERVAL_SEC = 30.0
KILL_DEVICE_HOLDERS = True

# Optional [slots] pinning: {slot_index: pin_substring}. Parsed by
# _parse_slot_pins(); consumed by core.camera's assign_slots /
# choose_slot_for_identity. Empty when no [slots] section is configured.
SLOT_PINS: dict[int, str] = {}

PROFILE_CAPTURE_WIDTH = 640
PROFILE_CAPTURE_HEIGHT = 480
PROFILE_CAPTURE_FPS = 25
PROFILE_UI_FPS = 20

# GStreamer pipeline support
USE_GSTREAMER = True

# Render overhead compensation (ms)
RENDER_OVERHEAD_MS = 3


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def _as_bool(value: Any, default: bool) -> bool:
    """Parse a value as boolean."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _as_int(
    value: Any,
    default: int,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
) -> int:
    """Parse a value as integer with optional bounds."""
    try:
        if value is None:
            return default
        parsed = int(value)
    except Exception:
        return default
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed


def _as_float(
    value: Any,
    default: float,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
) -> float:
    """Parse a value as float with optional bounds."""
    try:
        if value is None:
            return default
        parsed = float(value)
    except Exception:
        return default
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed


def _parse_slot_pins(parser: configparser.ConfigParser) -> dict[int, str]:
    """Parse the optional [slots] pinning section into {slot_index: pin}.

    Keys must fullmatch `slot(\\d+)` with the index in
    [0, CAMERA_SLOT_COUNT - 1] (the already-applied value: call this
    AFTER the table loop has finalized CAMERA_SLOT_COUNT). Values are
    stripped and must be non-empty. Invalid key format, an out-of-range
    index, or an empty value are skipped with a warning. Two different
    slots pinned to the identical value are both kept (matching is
    resolved at assignment time), but logged since only one can actually
    match a camera.
    """
    pins: dict[int, str] = {}
    if not parser.has_section("slots"):
        return pins

    slot_by_value: dict[str, int] = {}
    for key, raw_value in parser.items("slots"):
        match = re.fullmatch(r"slot(\d+)", key)
        if not match:
            logging.warning("Ignoring [slots] key %r: expected 'slotN'", key)
            continue
        index = int(match.group(1))
        if not (0 <= index < CAMERA_SLOT_COUNT):
            logging.warning(
                "Ignoring [slots] key %r: index out of range [0, %d)",
                key, CAMERA_SLOT_COUNT,
            )
            continue
        value = raw_value.strip()
        if not value:
            logging.warning("Ignoring [slots] key %r: empty value", key)
            continue
        if value in slot_by_value:
            logging.warning(
                "slot%d and slot%d are both pinned to %r; only one can match a camera",
                slot_by_value[value], index, value,
            )
        else:
            slot_by_value[value] = index
        pins[index] = value

    return pins


def load_config(path: Optional[str] = None) -> configparser.ConfigParser:
    """Load configuration from INI file."""
    if path is None:
        path = CONFIG_PATH
    parser = configparser.ConfigParser()
    if path and os.path.exists(path):
        parser.read(path)
    return parser


class _ConfigEntry(NamedTuple):
    """One row of the apply_config table:

    (section, key, global_name, converter, default, min_value, max_value)

    - section/key: where to read the value from the INI parser.
    - global_name: module-level global the parsed value is assigned to.
    - converter: _as_bool / _as_int / _as_float.
    - default: the global's defined default; used as a safety-net fallback
      if the global were ever missing (it shouldn't be - normal operation
      reads the global's *current* value, mirroring the original code's
      self-referential `fallback=GLOBAL` / `default=GLOBAL` pattern so
      re-applying config is sticky rather than resetting on a missing key).
    - min_value/max_value: bounds forwarded to _as_int/_as_float; unused
      (and not passed) for _as_bool entries, which take no bounds.
    """

    section: str
    key: str
    global_name: str
    converter: Callable[..., Any]
    default: Any
    min_value: Optional[float] = None
    max_value: Optional[float] = None


# Every config key that follows the read -> convert (+ optional bounds) ->
# assign-to-global pattern. Keys with special handling (raw string
# passthrough for LOG_LEVEL/LOG_FILE, the LOG_FILE_ENV override) are NOT
# listed here and stay as explicit code in apply_config below.
_CONFIG_TABLE: tuple[_ConfigEntry, ...] = (
    _ConfigEntry("logging", "max_bytes", "LOG_MAX_BYTES", _as_int, LOG_MAX_BYTES, min_value=1024),
    _ConfigEntry("logging", "backup_count", "LOG_BACKUP_COUNT", _as_int, LOG_BACKUP_COUNT, min_value=1),
    _ConfigEntry("logging", "stdout", "LOG_TO_STDOUT", _as_bool, LOG_TO_STDOUT),

    _ConfigEntry("performance", "dynamic_fps", "DYNAMIC_FPS_ENABLED", _as_bool, DYNAMIC_FPS_ENABLED),
    _ConfigEntry("performance", "perf_check_interval_ms", "PERF_CHECK_INTERVAL_MS", _as_int, PERF_CHECK_INTERVAL_MS, min_value=250),
    _ConfigEntry("performance", "min_dynamic_fps", "MIN_DYNAMIC_FPS", _as_int, MIN_DYNAMIC_FPS, min_value=1),
    _ConfigEntry("performance", "min_dynamic_ui_fps", "MIN_DYNAMIC_UI_FPS", _as_int, MIN_DYNAMIC_UI_FPS, min_value=1),
    _ConfigEntry("performance", "ui_fps_step", "UI_FPS_STEP", _as_int, UI_FPS_STEP, min_value=1),
    _ConfigEntry("performance", "cpu_load_threshold", "CPU_LOAD_THRESHOLD", _as_float, CPU_LOAD_THRESHOLD, min_value=0.1, max_value=1.0),
    _ConfigEntry("performance", "cpu_temp_threshold_c", "CPU_TEMP_THRESHOLD_C", _as_float, CPU_TEMP_THRESHOLD_C, min_value=30.0, max_value=100.0),
    _ConfigEntry("performance", "stress_hold_count", "STRESS_HOLD_COUNT", _as_int, STRESS_HOLD_COUNT, min_value=1),
    _ConfigEntry("performance", "recover_hold_count", "RECOVER_HOLD_COUNT", _as_int, RECOVER_HOLD_COUNT, min_value=1),
    _ConfigEntry("performance", "stale_frame_timeout_sec", "STALE_FRAME_TIMEOUT_SEC", _as_float, STALE_FRAME_TIMEOUT_SEC, min_value=0.5),
    _ConfigEntry("performance", "restart_cooldown_sec", "RESTART_COOLDOWN_SEC", _as_float, RESTART_COOLDOWN_SEC, min_value=1.0),
    _ConfigEntry("performance", "max_restarts_per_window", "MAX_RESTARTS_PER_WINDOW", _as_int, MAX_RESTARTS_PER_WINDOW, min_value=1),
    _ConfigEntry("performance", "restart_window_sec", "RESTART_WINDOW_SEC", _as_float, RESTART_WINDOW_SEC, min_value=5.0),

    _ConfigEntry("camera", "rescan_interval_ms", "RESCAN_INTERVAL_MS", _as_int, RESCAN_INTERVAL_MS, min_value=500),
    _ConfigEntry("camera", "failed_camera_cooldown_sec", "FAILED_CAMERA_COOLDOWN_SEC", _as_float, FAILED_CAMERA_COOLDOWN_SEC, min_value=1.0),
    _ConfigEntry("camera", "slot_count", "CAMERA_SLOT_COUNT", _as_int, CAMERA_SLOT_COUNT, min_value=1, max_value=8),
    _ConfigEntry("camera", "kill_device_holders", "KILL_DEVICE_HOLDERS", _as_bool, KILL_DEVICE_HOLDERS),
    _ConfigEntry("camera", "use_gstreamer", "USE_GSTREAMER", _as_bool, USE_GSTREAMER),

    _ConfigEntry("profile", "capture_width", "PROFILE_CAPTURE_WIDTH", _as_int, PROFILE_CAPTURE_WIDTH, min_value=160, max_value=1920),
    _ConfigEntry("profile", "capture_height", "PROFILE_CAPTURE_HEIGHT", _as_int, PROFILE_CAPTURE_HEIGHT, min_value=120, max_value=1080),
    _ConfigEntry("profile", "capture_fps", "PROFILE_CAPTURE_FPS", _as_int, PROFILE_CAPTURE_FPS, min_value=1, max_value=60),
    _ConfigEntry("profile", "ui_fps", "PROFILE_UI_FPS", _as_int, PROFILE_UI_FPS, min_value=1, max_value=60),

    _ConfigEntry("health", "log_interval_sec", "HEALTH_LOG_INTERVAL_SEC", _as_float, HEALTH_LOG_INTERVAL_SEC, min_value=5.0),
)


def apply_config(parser: configparser.ConfigParser) -> None:
    """Apply loaded configuration to global settings."""
    # LOG_LEVEL/LOG_FILE are raw string passthroughs (no converter), so they
    # stay outside the table and need the `global` statement for direct
    # assignment. The table loop below assigns via globals()[...] instead.
    global LOG_LEVEL, LOG_FILE, SLOT_PINS

    if parser.has_section("logging"):
        LOG_LEVEL = parser.get("logging", "level", fallback=LOG_LEVEL)
        LOG_FILE = parser.get("logging", "file", fallback=LOG_FILE)

    for entry in _CONFIG_TABLE:
        if not parser.has_section(entry.section):
            continue
        current = globals().get(entry.global_name, entry.default)
        raw = parser.get(entry.section, entry.key, fallback=current)
        if entry.converter is _as_bool:
            value = entry.converter(raw, current)
        else:
            value = entry.converter(
                raw, current, min_value=entry.min_value, max_value=entry.max_value
            )
        globals()[entry.global_name] = value

    # SLOT_PINS is a custom parser (not a converter table row) that must
    # run AFTER the loop above so CAMERA_SLOT_COUNT is final; always
    # assigned as a fresh dict, never mutated in place.
    SLOT_PINS = _parse_slot_pins(parser)

    if LOG_FILE_ENV:
        LOG_FILE = LOG_FILE_ENV


def configure_logging() -> None:
    """Set up logging handlers based on configuration."""
    level_name = (LOG_LEVEL or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = []

    if LOG_FILE:
        log_dir = os.path.dirname(LOG_FILE)
        try:
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            file_handler = RotatingFileHandler(
                LOG_FILE,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:
            logging.warning("Failed to configure file logging: %s", exc)

    if LOG_TO_STDOUT:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    logging.captureWarnings(True)


def choose_profile() -> tuple[int, int, int, int]:
    """Pick capture resolution and FPS from configuration.

    Resolution and FPS are exactly as configured in config.ini.
    Dynamic FPS feature will adjust up/down at runtime based on CPU load.

    Returns: (width, height, capture_fps, ui_fps)
    """
    # Exact values from config - no scaling
    return (PROFILE_CAPTURE_WIDTH, PROFILE_CAPTURE_HEIGHT, PROFILE_CAPTURE_FPS, PROFILE_UI_FPS)
