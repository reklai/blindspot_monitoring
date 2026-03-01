# Blindspot monitoring

A multi-camera blindspot monitoring system optimized for Raspberry Pi, designed for blind-spot monitoring on cargo vehicles. Features real-time video streaming with GStreamer acceleration, dynamic performance tuning, hot-plug support.

![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)
![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi%20%7C%20Linux-lightgrey.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

---

## Features

### Multi-Camera Support

- Automatically detects and displays up to 3 USB cameras simultaneously
- Smart grid layout adapts to available camera count
- Hot-plug support - cameras can be connected/disconnected at runtime

### Interactive Interface

- **Touch/Mouse Controls**: Single tap/click for fullscreen, long press to swap positions
- **Swap Mode**: Reorganize camera layout with intuitive gestures
- **Night Mode**: Toggle enhanced visibility for low-light conditions
- **Settings Tile**: Quick access to restart, night mode, and exit

### Performance Optimization

- **GStreamer Pipeline**: Hardware-accelerated MJPEG decoding with jpegdec (with V4L2 fallback)
- **Dynamic FPS Adjustment**: Automatically reduces frame rate under CPU/thermal stress
- **Threaded Architecture**: Separate capture threads ensure smooth UI performance
- **Efficient Rendering**: Configurable UI refresh rate (default 20 FPS in config.ini) balances smoothness and CPU usage

### System Integration

- **Auto-Start on Boot**: Systemd user service launches the app automatically when the Pi powers on
- **Auto-Restart**: If the app crashes or is accidentally closed, systemd restarts it within 5 seconds
- **Desktop Shortcut**: Manual launch fallback from desktop
- **Zero Configuration**: Works out of the box with standard USB cameras
- **Robust Recovery**: Automatic camera reconnection with exponential backoff

---

## System Requirements

### Supported Platforms

- **Raspberry Pi 5** (recommended)
- **Raspberry Pi 4** (64-bit OS recommended)
- **Linux** (Ubuntu 22.04+, Debian 12+)

### Hardware Tested Against

- USB webcams compatible with V4L2 (MJPEG support recommended)
- Minimum 2GB RAM (4GB+ recommended for 3 cameras)
- Display with X11 or Wayland

### Software Minimum Dependencies

- Python 3.9+
- PyQt6 (Qt6 GUI framework)
- OpenCV (with GStreamer support)
- GStreamer 1.0 (for optimized capture with jpegdec)
- pytest, pytest-qt (for running tests)

---

## Quick Start

```bash
# Clone and install
git clone https://github.com/Reece-Reklai/camera_dashboard.git
cd camera_dashboard
chmod +x install.sh
./install.sh

# Run the application
source .venv/bin/activate
python3 main.py
```

## Installation

### Automated Installation (Recommended)

```bash
chmod +x install.sh
./install.sh
```

The installer will:

1. Update system packages
2. Install system dependencies (PyQt6, OpenCV, GStreamer)
3. Create a Python virtual environment with system-site-packages
4. Configure camera permissions (adds user to `video` group)
5. Create a desktop shortcut for easy launching
6. Install a systemd user service for auto-start on boot
7. Enable user linger so the service starts without manual login

### Manual Installation

<details>
<summary>Click to expand manual steps</summary>

#### 1. Update System

```bash
sudo apt update && sudo apt upgrade -y
```

#### 2. Install System Dependencies

```bash
sudo apt install -y \
  python3 python3-pip python3-venv \
  python3-pyqt6 python3-opencv python3-numpy \
  libgl1 libegl1 libxkbcommon0 libxkbcommon-x11-0 \
  libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
  libxcb-render-util0 libxcb-xinerama0 libxcb-xfixes0 \
  libqt6gui6 libqt6widgets6 v4l-utils \
  gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
```

#### 3. Create Virtual Environment

```bash
# Use --system-site-packages to access system PyQt6/OpenCV
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
```

#### 4. Fix Camera Permissions

```bash
sudo usermod -aG video $USER
# Log out and back in for changes to take effect
```

#### 5. Create Logs Directory

```bash
mkdir -p logs
```

</details>

---

## Usage

### Auto-Start (Default)

After running `install.sh`, the app launches automatically on boot. No interaction required -- just power on the Pi.

The systemd service will also auto-restart the app if it crashes or is accidentally closed.

### Managing the Service

```bash
# Check status
systemctl --user status camera-dashboard

# Stop the app
systemctl --user stop camera-dashboard

# Start the app
systemctl --user start camera-dashboard

# Restart the app
systemctl --user restart camera-dashboard

# View live service logs
journalctl --user -u camera-dashboard -f

# Disable auto-start on boot
systemctl --user disable camera-dashboard
```

### Running Manually

```bash
# Manual run from terminal (stop the service first)
systemctl --user stop camera-dashboard
source .venv/bin/activate
python3 main.py

# Or double-click the desktop shortcut
# ~/Desktop/CameraDashboard.desktop
```

### Controls

| Action                   | Result                              |
| ------------------------ | ----------------------------------- |
| **Short Click/Tap**      | Toggle fullscreen view              |
| **Long Press (400ms+)**  | Enter swap mode (yellow border)     |
| **Click Another Camera** | Swap positions with selected camera |
| **Q**          | Exit application                    |
| **Ctrl+C**               | Exit from terminal                  |

### Camera Status Indicators

| Border Color    | Status                         |
| --------------- | ------------------------------ |
| Gray            | Camera connected and streaming |
| Yellow          | Camera selected for swapping   |
| "DISCONNECTED"  | No camera detected in slot     |
| "CONNECTING..." | Camera being initialized       |

### Settings Tile (Top-Left)

- **Restart**: Restart the application
- **Nightmode**: Toggle night vision mode (red-tinted, enhanced brightness)
- **Exit**: Close the application

---

## Configuration Settings

### Config File: `config.ini`

```ini
[logging]
level = INFO                          # DEBUG, INFO, WARNING, ERROR
file = ./logs/camera_dashboard.log
max_bytes = 5242880                   # 5MB log rotation
backup_count = 3
stdout = true

[performance]
dynamic_fps = true                    # Auto-adjust FPS under stress
perf_check_interval_ms = 2000         # How often to check system load
min_dynamic_fps = 10                  # Minimum capture FPS
min_dynamic_ui_fps = 12               # Minimum UI render FPS
ui_fps_step = 2                       # FPS adjustment step size
cpu_load_threshold = 0.75             # 75% CPU triggers FPS reduction
cpu_temp_threshold_c = 75.0           # 75°C triggers FPS reduction
stress_hold_count = 3                 # Consecutive stress readings before reducing
recover_hold_count = 3                # Consecutive normal readings before restoring
stale_frame_timeout_sec = 1.5         # Seconds before frame considered stale
restart_cooldown_sec = 5.0            # Minimum time between camera restarts
max_restarts_per_window = 3           # Max restarts before giving up
restart_window_sec = 30.0             # Time window for restart counting

[camera]
rescan_interval_ms = 15000            # Hot-plug detection interval (15s)
failed_camera_cooldown_sec = 30.0     # Retry delay for failed cameras
slot_count = 3                        # Number of camera slots
kill_device_holders = true            # Kill processes blocking cameras
use_gstreamer = true                  # Use GStreamer for capture (faster)

[profile]
capture_width = 640
capture_height = 480
capture_fps = 25                      # Camera capture rate
ui_fps = 20                           # UI refresh rate

[health]
log_interval_sec = 30                 # Health log frequency
```

### Environment Variables

```bash
# Override config file path
export CAMERA_DASHBOARD_CONFIG=/path/to/config.ini

# Override log file path
export CAMERA_DASHBOARD_LOG_FILE=/path/to/app.log
```

---

## Testing

### Running Tests through pytest

```bash
# Run all tests
./test.sh

# Run with verbose output
./test.sh -v

# Run specific test file
./test.sh tests/test_config.py

# Run tests matching a pattern
./test.sh -k "fullscreen"

# Run with short traceback
./test.sh --tb=short
```

### Test Coverage

| Test File | Tests | Coverage |
|-----------|-------|----------|
| `test_config.py` | 20 | Config parsing, validation, defaults |
| `test_camera.py` | 13 | Camera discovery, capture worker, GStreamer |
| `test_widgets.py` | 18 | Widget lifecycle, fullscreen, night mode |
| `test_helpers.py` | 16 | Utility functions, process management |
| **Total** | **67** | |

### Manual Test Run

```bash
# Activate virtual environment first
source .venv/bin/activate

# Run pytest directly
python -m pytest tests/ -v

# Run with coverage (requires pytest-cov)
pip install pytest-cov
python -m pytest tests/ --cov=core --cov=ui --cov=utils
```

---

## Performance Metrics

### Raspberry Pi 5 Benchmarks

| Cameras | Resolution | Capture FPS | UI FPS | CPU Usage | Memory |
| ------- | ---------- | ----------- | ------ | --------- | ------ |
| 1       | 640x480    | 25          | 20     | ~15%      | ~150MB |
| 2       | 640x480    | 22          | 18     | ~25%      | ~180MB |
| 3       | 640x480    | 22          | 18     | ~35%      | ~200MB |

---

## Troubleshooting common issues

### Cameras Not Detected

```bash
# Check if cameras are recognized
ls -l /dev/video*
v4l2-ctl --list-devices

# Verify user is in video group
groups $USER

# Test camera directly
ffplay /dev/video0
```

### GStreamer Issues

```bash
# Test GStreamer pipeline
gst-launch-1.0 v4l2src device=/dev/video0 ! jpegdec ! videoconvert ! autovideosink

# Disable GStreamer in config.ini
use_gstreamer = false
```

### Application Crashes / Unknown behavior, errors

```bash
# Check logs
cat logs/camera_dashboard.log | tail -50

# Check systemd service logs
journalctl --user -u camera-dashboard --since "10 minutes ago" --no-pager

# Run with debug logging level
# Edit config.ini and set: level = DEBUG
```

### Service Not Starting on Boot

```bash
# Verify service is enabled
systemctl --user is-enabled camera-dashboard

# Check if user linger is active
loginctl show-user $(whoami) -p Linger

# Re-enable if needed
systemctl --user enable camera-dashboard
sudo loginctl enable-linger $(whoami)
```

---

## Architecture

### Component Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Main Application                      │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │ CameraWidget│  │ CameraWidget│  │ CameraWidget│     │
│  │   + Worker  │  │   + Worker  │  │   + Worker  │     │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘     │
│         │                │                │             │
│  ┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐     │
│  │CaptureWorker│  │CaptureWorker│  │CaptureWorker│     │
│  │  (QThread)  │  │  (QThread)  │  │  (QThread)  │     │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘     │
│         │                │                │             │
│  ┌──────▼──────────────────────────────────▼──────┐    │
│  │           GStreamer / V4L2 Backend             │    │
│  └────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

### Threading Model

- **Main Thread**: Qt event loop, UI rendering
- **Capture Threads**: One QThread per camera for frame capture
- **Timer Callbacks**: Performance monitoring, health logging, device rescanning

### Data Flow

1. `CaptureWorker` grabs frames via GStreamer or V4L2
2. Frames emitted to main thread via Qt signals
3. UI renders at fixed interval (configurable; 20 FPS default in config.ini) using latest frame
4. Performance monitor adjusts FPS based on system load

---

## File Structure

```
camera_dashboard/
├── main.py                   # Application entry point
├── core/                     # Core functionality
│   ├── __init__.py           # Exports: config, camera, performance
│   ├── config.py             # Configuration loading, logging setup, constants
│   ├── camera.py             # CaptureWorker thread, camera discovery
│   └── performance.py        # CPU load/temp monitoring, stress detection
├── ui/                       # User interface
│   ├── __init__.py           # Exports: CameraWidget, get_smart_grid
│   ├── widgets.py            # CameraWidget, FullscreenOverlay
│   └── layout.py             # Grid layout helpers
├── utils/                    # Utilities
│   ├── __init__.py           # Exports: system helpers
│   └── helpers.py            # Process management, health logging
├── tests/                    # Test suite
│   ├── __init__.py           # Test package marker
│   ├── conftest.py           # Pytest fixtures
│   ├── test_config.py        # Config tests
│   ├── test_camera.py        # Camera tests
│   ├── test_widgets.py       # Widget tests
│   └── test_helpers.py       # Helper function tests
├── config.ini                # Configuration file
├── install.sh                # Automated installer
├── test.sh                   # Test runner script
├── pytest.ini                # Pytest configuration
├── requirements.txt          # Python dependencies
├── README.md                 # This file
├── LICENSE.MIT               # MIT License
├── logs/                     # Log files (created at runtime)
│   └── camera_dashboard.log
└── .venv/                    # Python virtual environment
```

### Module Overview

| Module | Description |
| ------ | ----------- |
| `core.config` | Configuration loading from INI, environment variables, logging setup |
| `core.camera` | `CaptureWorker` QThread for video capture, camera discovery functions |
| `core.performance` | CPU load and temperature monitoring, stress detection |
| `ui.widgets` | `CameraWidget` for camera tiles, `FullscreenOverlay` for fullscreen view |
| `ui.layout` | Grid layout calculation based on camera count |
| `utils.helpers` | System utilities, process management, health logging |

---

## License

This project is licensed under the MIT License - see [LICENSE.MIT](LICENSE.MIT) for details.

---
