"""
Configuration management for Camera Dashboard.

Handles loading config from INI files, environment variables,
and provides default values for all settings.
"""

from __future__ import annotations

import configparser
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Any, Optional


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


def load_config(path: Optional[str] = None) -> configparser.ConfigParser:
    """Load configuration from INI file."""
    if path is None:
        path = CONFIG_PATH
    parser = configparser.ConfigParser()
    if path and os.path.exists(path):
        parser.read(path)
    return parser


def apply_config(parser: configparser.ConfigParser) -> None:
    """Apply loaded configuration to global settings."""
    global LOG_LEVEL, LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT, LOG_TO_STDOUT
    global DYNAMIC_FPS_ENABLED, PERF_CHECK_INTERVAL_MS, MIN_DYNAMIC_FPS
    global MIN_DYNAMIC_UI_FPS, UI_FPS_STEP, CPU_LOAD_THRESHOLD, CPU_TEMP_THRESHOLD_C
    global STRESS_HOLD_COUNT, RECOVER_HOLD_COUNT, STALE_FRAME_TIMEOUT_SEC
    global RESTART_COOLDOWN_SEC, MAX_RESTARTS_PER_WINDOW, RESTART_WINDOW_SEC
    global RESCAN_INTERVAL_MS, FAILED_CAMERA_COOLDOWN_SEC, CAMERA_SLOT_COUNT
    global HEALTH_LOG_INTERVAL_SEC, KILL_DEVICE_HOLDERS
    global PROFILE_CAPTURE_WIDTH, PROFILE_CAPTURE_HEIGHT, PROFILE_CAPTURE_FPS
    global PROFILE_UI_FPS, USE_GSTREAMER

    if parser.has_section("logging"):
        LOG_LEVEL = parser.get("logging", "level", fallback=LOG_LEVEL)
        LOG_FILE = parser.get("logging", "file", fallback=LOG_FILE)
        LOG_MAX_BYTES = _as_int(
            parser.get("logging", "max_bytes", fallback=LOG_MAX_BYTES),
            LOG_MAX_BYTES,
            min_value=1024,
        )
        LOG_BACKUP_COUNT = _as_int(
            parser.get("logging", "backup_count", fallback=LOG_BACKUP_COUNT),
            LOG_BACKUP_COUNT,
            min_value=1,
        )
        LOG_TO_STDOUT = _as_bool(
            parser.get("logging", "stdout", fallback=LOG_TO_STDOUT), LOG_TO_STDOUT
        )

    if parser.has_section("performance"):
        DYNAMIC_FPS_ENABLED = _as_bool(
            parser.get("performance", "dynamic_fps", fallback=DYNAMIC_FPS_ENABLED),
            DYNAMIC_FPS_ENABLED,
        )
        PERF_CHECK_INTERVAL_MS = _as_int(
            parser.get(
                "performance", "perf_check_interval_ms", fallback=PERF_CHECK_INTERVAL_MS
            ),
            PERF_CHECK_INTERVAL_MS,
            min_value=250,
        )
        MIN_DYNAMIC_FPS = _as_int(
            parser.get("performance", "min_dynamic_fps", fallback=MIN_DYNAMIC_FPS),
            MIN_DYNAMIC_FPS,
            min_value=1,
        )
        MIN_DYNAMIC_UI_FPS = _as_int(
            parser.get(
                "performance", "min_dynamic_ui_fps", fallback=MIN_DYNAMIC_UI_FPS
            ),
            MIN_DYNAMIC_UI_FPS,
            min_value=1,
        )
        UI_FPS_STEP = _as_int(
            parser.get("performance", "ui_fps_step", fallback=UI_FPS_STEP),
            UI_FPS_STEP,
            min_value=1,
        )
        CPU_LOAD_THRESHOLD = _as_float(
            parser.get(
                "performance", "cpu_load_threshold", fallback=CPU_LOAD_THRESHOLD
            ),
            CPU_LOAD_THRESHOLD,
            min_value=0.1,
            max_value=1.0,
        )
        CPU_TEMP_THRESHOLD_C = _as_float(
            parser.get(
                "performance", "cpu_temp_threshold_c", fallback=CPU_TEMP_THRESHOLD_C
            ),
            CPU_TEMP_THRESHOLD_C,
            min_value=30.0,
            max_value=100.0,
        )
        STRESS_HOLD_COUNT = _as_int(
            parser.get("performance", "stress_hold_count", fallback=STRESS_HOLD_COUNT),
            STRESS_HOLD_COUNT,
            min_value=1,
        )
        RECOVER_HOLD_COUNT = _as_int(
            parser.get(
                "performance", "recover_hold_count", fallback=RECOVER_HOLD_COUNT
            ),
            RECOVER_HOLD_COUNT,
            min_value=1,
        )
        STALE_FRAME_TIMEOUT_SEC = _as_float(
            parser.get(
                "performance",
                "stale_frame_timeout_sec",
                fallback=STALE_FRAME_TIMEOUT_SEC,
            ),
            STALE_FRAME_TIMEOUT_SEC,
            min_value=0.5,
        )
        RESTART_COOLDOWN_SEC = _as_float(
            parser.get(
                "performance", "restart_cooldown_sec", fallback=RESTART_COOLDOWN_SEC
            ),
            RESTART_COOLDOWN_SEC,
            min_value=1.0,
        )
        MAX_RESTARTS_PER_WINDOW = _as_int(
            parser.get(
                "performance",
                "max_restarts_per_window",
                fallback=MAX_RESTARTS_PER_WINDOW,
            ),
            MAX_RESTARTS_PER_WINDOW,
            min_value=1,
        )
        RESTART_WINDOW_SEC = _as_float(
            parser.get(
                "performance", "restart_window_sec", fallback=RESTART_WINDOW_SEC
            ),
            RESTART_WINDOW_SEC,
            min_value=5.0,
        )

    if parser.has_section("camera"):
        RESCAN_INTERVAL_MS = _as_int(
            parser.get("camera", "rescan_interval_ms", fallback=RESCAN_INTERVAL_MS),
            RESCAN_INTERVAL_MS,
            min_value=500,
        )
        FAILED_CAMERA_COOLDOWN_SEC = _as_float(
            parser.get(
                "camera",
                "failed_camera_cooldown_sec",
                fallback=FAILED_CAMERA_COOLDOWN_SEC,
            ),
            FAILED_CAMERA_COOLDOWN_SEC,
            min_value=1.0,
        )
        CAMERA_SLOT_COUNT = _as_int(
            parser.get("camera", "slot_count", fallback=CAMERA_SLOT_COUNT),
            CAMERA_SLOT_COUNT,
            min_value=1,
            max_value=8,
        )
        KILL_DEVICE_HOLDERS = _as_bool(
            parser.get("camera", "kill_device_holders", fallback=KILL_DEVICE_HOLDERS),
            KILL_DEVICE_HOLDERS,
        )
        USE_GSTREAMER = _as_bool(
            parser.get("camera", "use_gstreamer", fallback=USE_GSTREAMER), USE_GSTREAMER
        )

    if parser.has_section("profile"):
        PROFILE_CAPTURE_WIDTH = _as_int(
            parser.get("profile", "capture_width", fallback=PROFILE_CAPTURE_WIDTH),
            PROFILE_CAPTURE_WIDTH,
            min_value=160,
            max_value=1920,
        )
        PROFILE_CAPTURE_HEIGHT = _as_int(
            parser.get("profile", "capture_height", fallback=PROFILE_CAPTURE_HEIGHT),
            PROFILE_CAPTURE_HEIGHT,
            min_value=120,
            max_value=1080,
        )
        PROFILE_CAPTURE_FPS = _as_int(
            parser.get("profile", "capture_fps", fallback=PROFILE_CAPTURE_FPS),
            PROFILE_CAPTURE_FPS,
            min_value=1,
            max_value=60,
        )
        PROFILE_UI_FPS = _as_int(
            parser.get("profile", "ui_fps", fallback=PROFILE_UI_FPS),
            PROFILE_UI_FPS,
            min_value=1,
            max_value=60,
        )

    if parser.has_section("health"):
        HEALTH_LOG_INTERVAL_SEC = _as_float(
            parser.get("health", "log_interval_sec", fallback=HEALTH_LOG_INTERVAL_SEC),
            HEALTH_LOG_INTERVAL_SEC,
            min_value=5.0,
        )

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


def choose_profile(camera_count: int) -> tuple[int, int, int, int]:
    """Pick capture resolution and FPS based on camera count.
    
    Resolution and FPS are exactly as configured in config.ini.
    Dynamic FPS feature will adjust up/down at runtime based on CPU load.
    
    Returns: (width, height, capture_fps, ui_fps)
    """
    # Exact values from config - no scaling
    return (PROFILE_CAPTURE_WIDTH, PROFILE_CAPTURE_HEIGHT, PROFILE_CAPTURE_FPS, PROFILE_UI_FPS)
