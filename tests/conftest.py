"""
Pytest configuration and shared fixtures for Camera Dashboard tests.
"""

import os
import sys
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def temp_config_file() -> Generator[Path, None, None]:
    """Create a temporary config file for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False) as f:
        f.write("""
[logging]
level = INFO
file = ./logs/test.log
max_bytes = 1048576
backup_count = 2
stdout = false

[performance]
dynamic_fps = true
perf_check_interval_ms = 2000
min_dynamic_fps = 5
min_dynamic_ui_fps = 12
ui_fps_step = 2
cpu_load_threshold = 0.75
cpu_temp_threshold_c = 75.0
stress_hold_count = 3
recover_hold_count = 3
stale_frame_timeout_sec = 1.5
restart_cooldown_sec = 5.0
max_restarts_per_window = 3
restart_window_sec = 30.0

[camera]
rescan_interval_ms = 15000
failed_camera_cooldown_sec = 30.0
slot_count = 3
kill_device_holders = false
use_gstreamer = true

[profile]
capture_width = 640
capture_height = 480
capture_fps = 20
ui_fps = 15

[health]
log_interval_sec = 30
""")
        f.flush()
        yield Path(f.name)
    os.unlink(f.name)


@pytest.fixture
def mock_video_capture():
    """Mock cv2.VideoCapture for testing without real cameras."""
    with patch("cv2.VideoCapture") as mock_cap:
        instance = MagicMock()
        instance.isOpened.return_value = True
        instance.read.return_value = (True, MagicMock())
        instance.get.return_value = 30.0
        instance.set.return_value = True
        instance.release.return_value = None
        mock_cap.return_value = instance
        yield mock_cap


# Config globals that apply_config and tests may mutate.
_CONFIG_GLOBALS = [
    "LOG_LEVEL", "LOG_FILE", "LOG_MAX_BYTES", "LOG_BACKUP_COUNT", "LOG_TO_STDOUT",
    "DYNAMIC_FPS_ENABLED", "PERF_CHECK_INTERVAL_MS", "MIN_DYNAMIC_FPS",
    "MIN_DYNAMIC_UI_FPS", "UI_FPS_STEP", "CPU_LOAD_THRESHOLD", "CPU_TEMP_THRESHOLD_C",
    "STRESS_HOLD_COUNT", "RECOVER_HOLD_COUNT", "STALE_FRAME_TIMEOUT_SEC",
    "RESTART_COOLDOWN_SEC", "MAX_RESTARTS_PER_WINDOW", "RESTART_WINDOW_SEC",
    "RESCAN_INTERVAL_MS", "FAILED_CAMERA_COOLDOWN_SEC", "CAMERA_SLOT_COUNT",
    "HEALTH_LOG_INTERVAL_SEC", "KILL_DEVICE_HOLDERS",
    "PROFILE_CAPTURE_WIDTH", "PROFILE_CAPTURE_HEIGHT", "PROFILE_CAPTURE_FPS",
    "PROFILE_UI_FPS", "USE_GSTREAMER",
]


@pytest.fixture(autouse=False)
def save_restore_config():
    """Save config globals before a test and restore them afterwards."""
    from core import config as _cfg
    saved = {name: getattr(_cfg, name) for name in _CONFIG_GLOBALS}
    yield
    for name, value in saved.items():
        setattr(_cfg, name, value)


@pytest.fixture(scope="session")
def qapp():
    """Create a QApplication instance for widget tests.
    
    This fixture is session-scoped to avoid creating multiple QApplication instances.
    """
    # Only import PyQt6 if running widget tests
    try:
        from PyQt6.QtWidgets import QApplication
        
        # Check if QApplication already exists
        app = QApplication.instance()
        if app is None:
            # Use offscreen platform for headless testing
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            app = QApplication([])
        yield app
    except ImportError:
        pytest.skip("PyQt6 not available")
