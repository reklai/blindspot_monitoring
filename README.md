# Blindspot Monitor Camera System

A Raspberry Pi/Linux multi-camera dashboard for blind-spot monitoring and other small vehicle or workshop camera-display setups. The app discovers V4L2 USB cameras, displays them in configurable slots, supports touch/mouse interaction, and can run unattended through a user-level systemd service.

![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)
![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi%20%7C%20Linux-lightgrey.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

## Overview

This project is a practical camera monitor for Linux systems, with Raspberry Pi as the main deployment target. It is designed for a fixed display where several USB camera feeds need to stay visible, recover from camera interruptions, and start automatically when the machine boots.

The software has been field-deployed for daily blind-spot monitoring use on cargo transport vehicles. It is still provided as open-source software, not as a certified safety system. Test it thoroughly for your own hardware, cameras, vehicle, display, power, and operating environment.

## Features

- Multi-camera PyQt6 dashboard.
- One capture thread per active camera.
- Configurable camera slot count, defaulting to 3 camera slots.
- Settings tile for restart, night mode, and brightness presets.
- Fullscreen camera view with click/tap interaction.
- Long-press tile swapping for rearranging the display.
- Runtime camera rescanning for hot-plug workflows.
- Stale-frame detection and bounded capture-worker restart attempts.
- Optional OpenCV GStreamer capture path with V4L2 fallback.
- Dynamic software FPS throttling based on CPU load and thermal state.
- INI configuration with environment variable overrides.
- User systemd service and desktop shortcut for dedicated installations.

## How It Works

The main window contains one settings tile plus `camera.slot_count` camera slots. With the default `slot_count = 3`, the dashboard starts as a 2x2 grid:

- Settings tile.
- Camera slot 1.
- Camera slot 2.
- Camera slot 3.

At startup, the app discovers cameras, tests available devices, and assigns working cameras to the configured slots in a deterministic order (see [Camera Slot Binding](#camera-slot-binding) below). Empty slots show `DISCONNECTED`.

During runtime, a rescan timer looks for new cameras and attaches them to empty slots. If a camera stops producing fresh frames, the widget marks it disconnected, restarts the capture worker with cooldown limits, and can eventually detach the camera so the slot can be reused.

## Camera Slot Binding

Cameras are identified by physical USB port rather than by whatever `/dev/videoN` number the kernel happens to hand out. At startup the app reads `/dev/v4l/by-path`, which exposes a stable name per physical port, and sorts cameras by that port path (natural sort, so `usb-0:1.2` sorts before `usb-0:1.10`). Slots fill in that order. Because the ordering comes from the physical port rather than enumeration order, it stays the same across reboots and simple replugs as long as cables stay in their ports. If a camera is unplugged and reconnected, it returns to the tile it previously held rather than taking whatever slot is next free.

An optional `[slots]` section in `config.ini` pins specific ports to specific tiles:

```ini
[slots]
slot0 = usb-0:1.1
slot1 = usb-0:1.2
slot2 = usb-0:1.3
```

Use the stable `usb-0:1.3`-style port tail, not the `platform-...` prefix that precedes it in the full by-path name -- that prefix differs between Raspberry Pi models. In fallback mode (no `/dev/v4l/by-path`, see below), pin by numeric device index instead, e.g. `slot0 = index:0`.

A pinned slot whose camera is absent stays an empty, reserved placeholder rather than being filled by a different camera -- so a driver never mistakes one camera's blind-spot view for another's.

Two things to watch for when maintaining a deployed unit:

- Adding or removing a USB hub changes port paths (e.g. `usb-0:1.3` behind a newly added hub becomes `usb-0:1.3.1`). Re-check pins after changing USB topology.
- On systems without `/dev/v4l/by-path` (for example, missing udev rules), the app falls back to plain numeric `/dev/video*` enumeration order and logs a one-time startup warning.

## Controls

| Action | Result |
| ------ | ------ |
| Short click/tap on camera tile | Toggle fullscreen view |
| Click/tap while fullscreen | Exit fullscreen view |
| Right click on camera tile | Toggle fullscreen view |
| Long press, 400 ms or more | Select a tile for swap mode |
| Click another tile while one is selected | Swap the two tile positions |
| Click selected tile again | Cancel swap mode |
| `Q` | Quit the application |
| `Ctrl+C` | Quit from a terminal run |

Swap mode is indicated with a yellow border. Placeholder/status labels currently include `DISCONNECTED` and `CONNECTING...`.

## Settings Tile

The settings tile starts in the top-left position. It can be moved with the same long-press swap behavior as the other tiles.

Implemented controls:

- `Restart`: restarts the current Python process.
- `Nightmode`: toggles red-tinted grayscale rendering with increased brightness.
- Brightness presets: `15%`, `60%`, `80%`, `100%`, and `150%`.

Note: the current brightness code clamps the effective minimum multiplier to `0.5`, so the `15%` preset behaves as 50% brightness.

## Capture Pipeline

Capture is implemented with OpenCV. When `camera.use_gstreamer = true`, OpenCV has GStreamer support, the host is Linux, and the camera is an integer device index, the app first tries this pipeline:

```text
v4l2src device=/dev/videoN !
image/jpeg,width=W,height=H !
queue max-size-buffers=2 leaky=downstream !
jpegdec !
videoconvert !
appsink drop=1 max-buffers=1 sync=false
```

If that path is unavailable or fails to open, the app falls back to V4L2 and tries MJPG, YUYV, then automatic format selection.

Important details:

- `jpegdec` is a software JPEG decoder.
- The current implementation does not use a hardware JPEG decoder.
- Dynamic FPS changes are software throttling of emitted frames and UI render rate.
- Runtime FPS changes do not reconfigure the camera device FPS after opening.

## Requirements

Runtime:

- Linux desktop session with X11 or Wayland.
- Python 3.9+.
- PyQt6.
- OpenCV.
- NumPy.
- V4L2-compatible camera devices.

Optional but recommended:

- OpenCV built with GStreamer support when `camera.use_gstreamer = true`.
- GStreamer 1.0 packages and good/bad plugins.
- `v4l-utils` for camera inspection and troubleshooting.

The PyPI `opencv-python` package normally does not include GStreamer support. On Raspberry Pi/Debian systems, this project is intended to use distro OpenCV packages.

## Installation

Clone the project:

```bash
git clone https://github.com/reklai/blindspot_monitoring.git
cd blindspot_monitoring
```

### Raspberry Pi / Dedicated Linux Install

The included installer is intended for a dedicated Raspberry Pi or kiosk-style Linux deployment.

```bash
chmod +x install.sh
sudo ./install.sh
```

The installer:

- Requires `sudo`.
- Installs system packages with `apt`.
- Uses system Python packages rather than creating `.venv`.
- Creates `logs/`.
- Creates `~/Desktop/CameraDashboard.desktop`.
- Installs and enables a user service named `camera-dashboard.service`.
- Enables linger for the invoking user.
- Kills processes currently using `/dev/video*`.
- Stops/disables common camera-holding services such as ZoneMinder, Motion, and mjpeg-streamer.
- On Raspberry Pi systems, may edit `/boot/firmware/config.txt` and EEPROM power settings.
- Reboots the machine at the end.

Skip package update/upgrade if needed:

```bash
sudo ./install.sh --skip-update
```

### Manual Run

After installing the required system dependencies:

```bash
python3 main.py
```

If using a virtual environment, create it with system site packages so it can see distro-provided PyQt6/OpenCV:

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python3 main.py
```

## Service Management

The installer creates a user systemd service named `camera-dashboard`.

```bash
systemctl --user status camera-dashboard
systemctl --user stop camera-dashboard
systemctl --user start camera-dashboard
systemctl --user restart camera-dashboard
journalctl --user -u camera-dashboard -f
systemctl --user disable camera-dashboard
```

The service uses `Restart=always` and `RestartSec=5`, so systemd restarts the app about 5 seconds after it exits or crashes. `StartLimitIntervalSec=0` disables systemd's crash-loop lockout, so it keeps retrying indefinitely rather than giving up after repeated failures. `KillSignal=SIGTERM` is caught by the app for a clean Qt shutdown on `systemctl stop`/reboot, same as `Ctrl+C`.

## Configuration

The app reads `config.ini` by default.

```bash
export CAMERA_DASHBOARD_CONFIG=/path/to/config.ini
export CAMERA_DASHBOARD_LOG_FILE=/path/to/camera_dashboard.log
```

Current checked-in defaults:

```ini
[logging]
level = WARNING
file = ./logs/camera_dashboard.log
max_bytes = 5242880
backup_count = 3
stdout = true

[performance]
dynamic_fps = true
perf_check_interval_ms = 2000
min_dynamic_fps = 10
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
first_frame_timeout_sec = 10.0

[camera]
rescan_interval_ms = 15000
failed_camera_cooldown_sec = 30.0
slot_count = 3
kill_device_holders = true
use_gstreamer = true

[profile]
capture_width = 640
capture_height = 480
capture_fps = 25
ui_fps = 20

[health]
log_interval_sec = 60

[slots]
#slot0 = usb-0:1.1
#slot1 = usb-0:1.2
#slot2 = usb-0:1.3
```

`level` defaults to `WARNING` -- the minimum level that still captures reconnect/stale/health warnings useful for post-incident diagnosis on a fielded unit. `[slots]` is commented out by default; see [Camera Slot Binding](#camera-slot-binding) above.

`profile.capture_fps` and `profile.ui_fps` are base values used for all camera counts. They are not scaled by camera count; dynamic FPS may adjust them at runtime under stress.

## Project Layout

```text
blindspot_monitoring/
├── main.py
├── core/
│   ├── camera.py
│   ├── config.py
│   └── performance.py
├── ui/
│   ├── layout.py
│   └── widgets.py
├── utils/
│   └── helpers.py
├── tests/
├── config.ini
├── install.sh
├── requirements.txt
└── test.sh
```

## Development

The repository includes unit tests and a helper script. The helper script expects a `.venv`, but the automated installer does not create one.

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install pytest pytest-qt
python3 -m pytest tests/
```

## Troubleshooting

Check device visibility and permissions:

```bash
ls -l /dev/video*
v4l2-ctl --list-devices
groups "$USER"
```

Test a camera directly:

```bash
ffplay /dev/video0
```

Test a similar GStreamer path:

```bash
gst-launch-1.0 v4l2src device=/dev/video0 ! jpegdec ! videoconvert ! autovideosink
```

Disable the GStreamer capture path:

```ini
[camera]
use_gstreamer = false
```

Check logs:

```bash
tail -50 logs/camera_dashboard.log
journalctl --user -u camera-dashboard --since "10 minutes ago" --no-pager
```

## License

This project is licensed under the MIT License. See [LICENSE.MIT](LICENSE.MIT).
