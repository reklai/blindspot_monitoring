#!/usr/bin/env bash
set -euo pipefail

# Camera Dashboard installer for Raspberry Pi / Linux
# Run with sudo (will reboot automatically):
#   chmod +x install.sh
#   sudo ./install.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------- 0) parse args ----------
SKIP_UPDATE=false

for arg in "$@"; do
  case $arg in
    --skip-update) SKIP_UPDATE=true ;;
  esac
done

# ---------- 0) basic checks ----------

if [[ "$EUID" -ne 0 ]]; then
  echo "This script must be run with sudo."
  echo "Usage: sudo ./install.sh"
  echo "Options: --skip-update"
  exit 1
fi

SUDO_USER="${SUDO_USER:-$(whoami)}"

echo_section() {
  echo
  echo "========================================"
  echo "$1"
  echo "========================================"
}

# ---------- 1) update the system ----------

if [[ "$SKIP_UPDATE" == "false" ]]; then
  echo_section "1) Updating system packages"
  apt update
  apt upgrade -y
else
  echo_section "1) Skipping update (--skip-update)"
fi

# ---------- 2) install system dependencies ----------

echo_section "2) Installing system dependencies"

apt install -y \
  python3 python3-pip \
  python3-pyqt6 python3-opencv python3-numpy \
  libgl1 libegl1 libxkbcommon0 libxkbcommon-x11-0 \
  libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
  libxcb-render-util0 libxcb-xinerama0 libxcb-xfixes0 \
  libqt6gui6 libqt6widgets6 \
  v4l-utils gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad || true

# ---------- 3) kill processes holding cameras ----------

echo_section "3) Clearing camera devices"

# Kill any process using any video device (generic approach)
for dev in /dev/video*; do
  fuser -k "$dev" 2>/dev/null || true
done

# Kill common camera-holding processes
killall -9 zmc zma zoneminder 2>/dev/null || true

# Disable ZoneMinder and other common camera services
for svc in zoneminder motion mjpeg-streamer; do
  if systemctl list-unit-files | grep -q "^$svc.service"; then
    echo "Disabling $svc..."
    systemctl stop "$svc" 2>/dev/null || true
    systemctl disable "$svc" 2>/dev/null || true
  fi
done

# ---------- 4) add user to video group ----------

echo_section "4) Adding user to video group"

if ! groups "$SUDO_USER" | grep -q "\bvideo\b"; then
  usermod -aG video "$SUDO_USER"
  echo "Added $SUDO_USER to video group."
else
  echo "$SUDO_USER is already in video group."
fi

# ---------- 5) create logs directory ----------

echo_section "5) Creating logs directory"
mkdir -p "$SCRIPT_DIR/logs"

# ---------- 6) create desktop shortcut ----------

echo_section "6) Creating desktop shortcut"

DESKTOP_FILE="/home/$SUDO_USER/Desktop/CameraDashboard.desktop"
mkdir -p "$(dirname "$DESKTOP_FILE")"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Name=Camera Dashboard
Comment=Multi-camera monitoring dashboard
Exec=python3 $SCRIPT_DIR/main.py
Path=$SCRIPT_DIR
Icon=camera-video
Terminal=false
Type=Application
Categories=Video;Monitor;
StartupNotify=true
EOF

chown "$SUDO_USER:$SUDO_USER" "$DESKTOP_FILE"
chmod +x "$DESKTOP_FILE"

# ---------- 7) setup systemd service ----------

echo_section "7) Setting up systemd service"

SYSTEMD_DIR="/home/$SUDO_USER/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"
USER_UID="$(id -u "$SUDO_USER")"

cat > "$SYSTEMD_DIR/camera-dashboard.service" <<EOF
[Unit]
Description=Camera Dashboard
After=default.target
Wants=default.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStartPre=/bin/bash -c 'for dev in /dev/video*; do fuser -k "$dev" 2>/dev/null || true; done; killall -9 zmc zma zoneminder motion 2>/dev/null || true'
ExecStartPre=/bin/bash -c 'for i in \$(seq 1 30); do [ -e /run/user/$USER_UID/wayland-0 ] && exit 0; [ -e /tmp/.X11-unix/X0 ] && exit 0; sleep 1; done; exit 0'
ExecStart=python3 $SCRIPT_DIR/main.py
Environment=DISPLAY=:0
Environment=WAYLAND_DISPLAY=wayland-0
Environment=XDG_RUNTIME_DIR=/run/user/$USER_UID
Environment="QT_QPA_PLATFORM=wayland;xcb"
KillSignal=SIGTERM
TimeoutStopSec=10
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

chown -R "$SUDO_USER:$SUDO_USER" "$SYSTEMD_DIR"

# Enable service and linger - start user bus if needed
loginctl enable-linger "$SUDO_USER" 2>/dev/null || true
systemctl --machine="$SUDO_USER@" --user daemon-reload 2>/dev/null || \
  su - "$SUDO_USER" -c "XDG_RUNTIME_DIR=/run/user/$(id -u "$SUDO_USER") systemctl --user daemon-reload" 2>/dev/null || \
  echo "Note: User systemd may not be running (will start on login)"
su - "$SUDO_USER" -c "XDG_RUNTIME_DIR=/run/user/$(id -u "$SUDO_USER") systemctl --user enable camera-dashboard.service" 2>/dev/null || true

# ---------- 8) quick test ----------

echo_section "8) Quick test"

# Kill any processes using cameras before testing
for dev in /dev/video*; do
  fuser -k "$dev" 2>/dev/null || true
done
killall -9 zmc zma zoneminder motion 2>/dev/null || true

if timeout 10 python3 -c "
import cv2
cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
if cap.isOpened():
    ret, frame = cap.read()
    cap.release()
    if ret:
        print('Camera: OK')
    else:
        print('Camera: opened but no frame')
else:
    print('Camera: failed to open')
" 2>&1; then
  echo "Quick test passed!"
else
  echo "Quick test had issues (may be normal if no camera connected)"
fi

# ---------- done ----------

echo_section "Installation complete"

cat <<EOF

The app will auto-start on boot.

To start now:
  systemctl --user start camera-dashboard

To view logs:
  journalctl --user -u camera-dashboard -f

Controls:
  Click: Toggle fullscreen
  Q: Quit

EOF

echo "Rebooting in 5 seconds..."
sleep 5
reboot
