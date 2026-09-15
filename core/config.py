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

# Render overhead compensation (ms). Subtracted from the render timer
# period so the achieved UI rate lands on the target instead of just under
# it. Not an INI option; it is a property of the render path, not a site.
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
    numeric parsers; string and boolean options ignore them. ``doc`` is the
    operator-facing description: units, range, what it interacts with, and
    when a change takes effect. Every option needs one; it is the only place
    that knowledge lives.
    """

    section: str
    key: str
    name: str
    parse: Callable[..., Any]
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    doc: str = ""

    def coerce(self, raw: Any, current: Any) -> Any:
        if self.parse in (_as_int, _as_float):
            return self.parse(raw, current, self.min_value, self.max_value)
        return self.parse(raw, current)


_OPTIONS: tuple[_Option, ...] = (
    # ---- [logging] -------------------------------------------------------
    _Option("logging", "level", "LOG_LEVEL", _as_str, doc=(
        "Root log level: DEBUG, INFO, WARNING, ERROR or CRITICAL. Fielded units "
        "run ERROR; INFO adds a status line per camera every 10 s and the health "
        "summary, DEBUG adds every gesture and open attempt. Read at start-up only."
    )),
    _Option("logging", "file", "LOG_FILE", _as_str, doc=(
        "Path of the rotating log file, relative to the working directory. The "
        "CAMERA_DASHBOARD_LOG_FILE environment variable overrides it. Empty "
        "disables file logging."
    )),
    _Option("logging", "max_bytes", "LOG_MAX_BYTES", _as_int, 1024, doc=(
        "Size at which the log file rotates. Minimum 1024."
    )),
    _Option("logging", "backup_count", "LOG_BACKUP_COUNT", _as_int, 1, doc=(
        "Rotated files to keep. Total disk use is about max_bytes x (backup_count + 1)."
    )),
    _Option("logging", "stdout", "LOG_TO_STDOUT", _as_bool, doc=(
        "Also log to stdout, which under the systemd service means the journal."
    )),
    # ---- [performance] ---------------------------------------------------
    _Option("performance", "dynamic_fps", "DYNAMIC_FPS_ENABLED", _as_bool, doc=(
        "Lower capture and UI rates when the CPU is loaded or hot, and restore "
        "them when it calms. Off means the profile rates are used unconditionally."
    )),
    _Option("performance", "perf_check_interval_ms", "PERF_CHECK_INTERVAL_MS", _as_int, 250, doc=(
        "How often load and temperature are sampled. Each sample is one loadavg "
        "call and one sysfs read; 2000 is plenty because the load average itself "
        "only moves on a one-minute time constant. Minimum 250."
    )),
    _Option("performance", "min_dynamic_fps", "MIN_DYNAMIC_FPS", _as_int, 1, doc=(
        "Floor for the capture (emit) rate under stress, per camera. Frames are "
        "still grabbed at the camera's rate; only decode/emit is reduced."
    )),
    _Option("performance", "min_dynamic_ui_fps", "MIN_DYNAMIC_UI_FPS", _as_int, 1, doc=(
        "Floor for the per-tile render rate under stress."
    )),
    _Option("performance", "ui_fps_step", "UI_FPS_STEP", _as_int, 1, doc=(
        "Render-rate change per stress or recovery decision. The capture rate "
        "always steps by 2."
    )),
    _Option("performance", "cpu_load_threshold", "CPU_LOAD_THRESHOLD", _as_float, 0.1, 1.0, doc=(
        "1-minute load average divided by core count above which the system "
        "counts as stressed. 0.75 on a 4-core Pi means loadavg 3.0."
    )),
    _Option("performance", "cpu_temp_threshold_c", "CPU_TEMP_THRESHOLD_C", _as_float, 30.0, 100.0, doc=(
        "SoC temperature above which the system counts as stressed. The Pi "
        "firmware throttles the CPU itself at 80-85 C, so keep this below that "
        "to shed load before the hardware does."
    )),
    _Option("performance", "stress_hold_count", "STRESS_HOLD_COUNT", _as_int, 1, doc=(
        "Consecutive stressed samples before rates are lowered one step. With "
        "the 2 s interval, 3 means about 6 s of sustained stress."
    )),
    _Option("performance", "recover_hold_count", "RECOVER_HOLD_COUNT", _as_int, 1, doc=(
        "Consecutive calm samples before rates are raised one step."
    )),
    _Option("performance", "stale_frame_timeout_sec", "STALE_FRAME_TIMEOUT_SEC", _as_float, 0.5, doc=(
        "Seconds without a new frame before a tile shows DISCONNECTED and asks "
        "for a worker restart. Must exceed one frame period at the lowest "
        "dynamic rate (10 FPS = 0.1 s) by a wide margin. Minimum 0.5."
    )),
    _Option("performance", "restart_cooldown_sec", "RESTART_COOLDOWN_SEC", _as_float, 1.0, doc=(
        "Minimum spacing between two worker restarts of the same camera."
    )),
    _Option("performance", "max_restarts_per_window", "MAX_RESTARTS_PER_WINDOW", _as_int, 1, doc=(
        "Restarts allowed per camera within restart_window_sec before the budget "
        "is exhausted. An exhausted camera waits 2 x restart_window_sec, and if "
        "it has produced no frame by then its slot is freed for the rescan."
    )),
    _Option("performance", "restart_window_sec", "RESTART_WINDOW_SEC", _as_float, 5.0, doc=(
        "Window for max_restarts_per_window; also sets the extended cooldown "
        "(2x) after exhaustion. Minimum 5."
    )),
    # ---- [camera] ----------------------------------------------------------
    _Option("camera", "rescan_interval_ms", "RESCAN_INTERVAL_MS", _as_int, 500, doc=(
        "How often /dev/video* is checked for new cameras and failed cameras "
        "are considered for detach. Runs for the life of the process. Each "
        "probe of a new node can block its background thread for a second or "
        "two, so keep this in the seconds range. Minimum 500."
    )),
    _Option("camera", "failed_camera_cooldown_sec", "FAILED_CAMERA_COOLDOWN_SEC", _as_float, 1.0, doc=(
        "After a probe fails (including the metadata node every UVC camera "
        "exposes), the index is not probed again for this long."
    )),
    _Option("camera", "slot_count", "CAMERA_SLOT_COUNT", _as_int, 1, 8, doc=(
        "Camera tiles in the grid, in addition to the settings tile. The grid "
        "shape follows the total: 3 slots + settings = 2x2. Range 1-8."
    )),
    _Option("camera", "kill_device_holders", "KILL_DEVICE_HOLDERS", _as_bool, doc=(
        "At start-up, terminate other processes holding a camera that will not "
        "open (a crashed previous instance, motion, ffmpeg). Kiosk setting: "
        "never enable on a shared desktop. Never applied by the runtime rescan."
    )),
    # ---- [profile] ---------------------------------------------------------
    _Option("profile", "capture_width", "PROFILE_CAPTURE_WIDTH", _as_int, 160, 1920, doc=(
        "Requested capture width; the driver may pick the nearest mode it "
        "supports. Decode and every copy scale with width x height."
    )),
    _Option("profile", "capture_height", "PROFILE_CAPTURE_HEIGHT", _as_int, 120, 1080, doc=(
        "Requested capture height. See capture_width."
    )),
    _Option("profile", "capture_fps", "PROFILE_CAPTURE_FPS", _as_int, 1, 60, doc=(
        "Frames per second requested from the camera and emitted to the UI "
        "when unstressed. Applies to every camera; not scaled by camera count."
    )),
    _Option("profile", "ui_fps", "PROFILE_UI_FPS", _as_int, 1, 60, doc=(
        "Render rate per tile when unstressed. Rendering faster than "
        "capture_fps only repaints identical frames."
    )),
    # ---- [health] ----------------------------------------------------------
    _Option("health", "log_interval_sec", "HEALTH_LOG_INTERVAL_SEC", _as_float, 5.0, doc=(
        "Interval of the one-line health summary (online/stale/placeholder "
        "counts) at INFO. Minimum 5."
    )),
)


def option_docs() -> str:
    """Render every option's documentation as INI-style text (for tooling)."""
    lines: list[str] = []
    section = None
    for option in _OPTIONS:
        if option.section != section:
            section = option.section
            lines.append(f"[{section}]")
        lines.append(f"# {option.key}: {option.doc}")
    return "\n".join(lines)


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
    # Writing through globals() rather than a `global` statement per name
    # keeps the table the single place a setting is spelled out.
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
