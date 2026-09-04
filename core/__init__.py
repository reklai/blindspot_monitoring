"""Core modules for camera capture, configuration, and performance monitoring.

Configuration values are read as ``config.NAME`` at the time of use. They are
deliberately not re-exported here: a ``from core import NAME`` would copy the
default at import time and never see what ``apply_config`` loaded.
"""

__all__ = [
    "config",
    "load_config",
    "apply_config",
    "configure_logging",
    "choose_profile",
    "CaptureWorker",
    "find_working_cameras",
    "get_video_indexes",
    "test_single_camera",
    "is_system_stressed",
]

from . import config
from .config import apply_config, choose_profile, configure_logging, load_config
from .camera import (
    CaptureWorker,
    find_working_cameras,
    get_video_indexes,
    test_single_camera,
)
from .performance import is_system_stressed
