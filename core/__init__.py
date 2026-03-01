"""Core modules for camera capture, configuration, and performance monitoring."""

__all__ = [
    # config module exports
    "load_config",
    "apply_config",
    "configure_logging",
    "choose_profile",
    "config",
    "CONFIG_PATH",
    "CAMERA_SLOT_COUNT",
    "DYNAMIC_FPS_ENABLED",
    "PERF_CHECK_INTERVAL_MS",
    "MIN_DYNAMIC_FPS",
    "MIN_DYNAMIC_UI_FPS",
    "UI_FPS_STEP",
    "STRESS_HOLD_COUNT",
    "RECOVER_HOLD_COUNT",
    "RESCAN_INTERVAL_MS",
    "FAILED_CAMERA_COOLDOWN_SEC",
    "HEALTH_LOG_INTERVAL_SEC",
    # camera module exports
    "CaptureWorker",
    "find_working_cameras",
    "get_video_indexes",
    "test_single_camera",
    # performance module exports
    "is_system_stressed",
]

from .config import (
    load_config,
    apply_config,
    configure_logging,
    choose_profile,
    CONFIG_PATH,
    CAMERA_SLOT_COUNT,
    DYNAMIC_FPS_ENABLED,
    PERF_CHECK_INTERVAL_MS,
    MIN_DYNAMIC_FPS,
    MIN_DYNAMIC_UI_FPS,
    UI_FPS_STEP,
    STRESS_HOLD_COUNT,
    RECOVER_HOLD_COUNT,
    RESCAN_INTERVAL_MS,
    FAILED_CAMERA_COOLDOWN_SEC,
    HEALTH_LOG_INTERVAL_SEC,
)
from .camera import CaptureWorker, find_working_cameras, get_video_indexes, test_single_camera
from .performance import is_system_stressed
