"""
Configuration for Camera Dashboard.

Settings live as module-level globals so the rest of the code can read
``config.NAME`` cheaply from any thread and tests can monkeypatch a single
value. The INI schema is declared once in ``_OPTIONS``; ``apply_config``
walks that table, so adding a setting means adding one row and one default.

Precedence, lowest to highest: the defaults below, ``config.ini`` (or the
file named by ``CAMERA_DASHBOARD_CONFIG``), then ``CAMERA_DASHBOARD_LOG_FILE``
for the log path only.
"""

from __future__ import annotations

import configparser
import logging
import os
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Optional


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

PROFILE_CAPTURE_WIDTH = 640
PROFILE_CAPTURE_HEIGHT = 480
PROFILE_CAPTURE_FPS = 25
PROFILE_UI_FPS = 20

# GStreamer pipeline support
USE_GSTREAMER = True

# Render overhead compensation (ms)
RENDER_OVERHEAD_MS = 3


# ============================================================
# VALUE PARSERS
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


def _as_str(value: Any, default: str) -> str:
    """Keep a string setting as-is; only ``None`` falls back to the default."""
    return default if value is None else str(value)


# ============================================================
# INI SCHEMA
# ============================================================

@dataclass(frozen=True)
class _Option:
    """One INI key bound to one module global.

    ``parse`` receives the raw INI text and the current global value (used as
    the fallback for unparsable input). Bounds are passed through to the
    numeric parsers; string and boolean options ignore them.
    """

    section: str
    key: str
    name: str
    parse: Callable[..., Any]
    min_value: Optional[float] = None
    max_value: Optional[float] = None

    def coerce(self, raw: Any, current: Any) -> Any:
        if self.parse in (_as_int, _as_float):
            return self.parse(raw, current, self.min_value, self.max_value)
        return self.parse(raw, current)


_OPTIONS: tuple[_Option, ...] = (
    # [logging]
    _Option("logging", "level", "LOG_LEVEL", _as_str),
    _Option("logging", "file", "LOG_FILE", _as_str),
    _Option("logging", "max_bytes", "LOG_MAX_BYTES", _as_int, 1024),
    _Option("logging", "backup_count", "LOG_BACKUP_COUNT", _as_int, 1),
    _Option("logging", "stdout", "LOG_TO_STDOUT", _as_bool),
    # [performance]
    _Option("performance", "dynamic_fps", "DYNAMIC_FPS_ENABLED", _as_bool),
    _Option("performance", "perf_check_interval_ms", "PERF_CHECK_INTERVAL_MS", _as_int, 250),
    _Option("performance", "min_dynamic_fps", "MIN_DYNAMIC_FPS", _as_int, 1),
    _Option("performance", "min_dynamic_ui_fps", "MIN_DYNAMIC_UI_FPS", _as_int, 1),
    _Option("performance", "ui_fps_step", "UI_FPS_STEP", _as_int, 1),
    _Option("performance", "cpu_load_threshold", "CPU_LOAD_THRESHOLD", _as_float, 0.1, 1.0),
    _Option("performance", "cpu_temp_threshold_c", "CPU_TEMP_THRESHOLD_C", _as_float, 30.0, 100.0),
    _Option("performance", "stress_hold_count", "STRESS_HOLD_COUNT", _as_int, 1),
    _Option("performance", "recover_hold_count", "RECOVER_HOLD_COUNT", _as_int, 1),
    _Option("performance", "stale_frame_timeout_sec", "STALE_FRAME_TIMEOUT_SEC", _as_float, 0.5),
    _Option("performance", "restart_cooldown_sec", "RESTART_COOLDOWN_SEC", _as_float, 1.0),
    _Option("performance", "max_restarts_per_window", "MAX_RESTARTS_PER_WINDOW", _as_int, 1),
    _Option("performance", "restart_window_sec", "RESTART_WINDOW_SEC", _as_float, 5.0),
    # [camera]
    _Option("camera", "rescan_interval_ms", "RESCAN_INTERVAL_MS", _as_int, 500),
    _Option("camera", "failed_camera_cooldown_sec", "FAILED_CAMERA_COOLDOWN_SEC", _as_float, 1.0),
    _Option("camera", "slot_count", "CAMERA_SLOT_COUNT", _as_int, 1, 8),
    _Option("camera", "kill_device_holders", "KILL_DEVICE_HOLDERS", _as_bool),
    _Option("camera", "use_gstreamer", "USE_GSTREAMER", _as_bool),
    # [profile]
    _Option("profile", "capture_width", "PROFILE_CAPTURE_WIDTH", _as_int, 160, 1920),
    _Option("profile", "capture_height", "PROFILE_CAPTURE_HEIGHT", _as_int, 120, 1080),
    _Option("profile", "capture_fps", "PROFILE_CAPTURE_FPS", _as_int, 1, 60),
    _Option("profile", "ui_fps", "PROFILE_UI_FPS", _as_int, 1, 60),
    # [health]
    _Option("health", "log_interval_sec", "HEALTH_LOG_INTERVAL_SEC", _as_float, 5.0),
)


def load_config(path: Optional[str] = None) -> configparser.ConfigParser:
    """Load configuration from an INI file; a missing file yields an empty parser."""
    if path is None:
        path = CONFIG_PATH
    parser = configparser.ConfigParser()
    if path and os.path.exists(path):
        parser.read(path)
    return parser


def apply_config(parser: configparser.ConfigParser) -> None:
    """Copy every recognised INI value into the matching module global.

    Keys that are absent or unparsable leave the current value untouched, so
    calling this with a partial file only overrides what the file mentions.
    """
    module_globals = globals()
    for option in _OPTIONS:
        if not parser.has_section(option.section):
            continue
        current = module_globals[option.name]
        raw = parser.get(option.section, option.key, fallback=current)
        module_globals[option.name] = option.coerce(raw, current)

    if LOG_FILE_ENV:
        module_globals["LOG_FILE"] = LOG_FILE_ENV


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


def choose_profile(camera_count: int) -> tuple[int, int, int, int]:
    """Return the configured capture profile as (width, height, capture_fps, ui_fps).

    ``camera_count`` is accepted for call-site symmetry but does not scale the
    result: the profile is exactly what config.ini says, and dynamic FPS
    handles load at runtime.
    """
    return (
        PROFILE_CAPTURE_WIDTH,
        PROFILE_CAPTURE_HEIGHT,
        PROFILE_CAPTURE_FPS,
        PROFILE_UI_FPS,
    )
