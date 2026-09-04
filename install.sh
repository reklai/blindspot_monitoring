#!/usr/bin/env bash
#
# Camera Dashboard installer for a dedicated Raspberry Pi / Linux kiosk.
#
#   sudo ./install.sh [--skip-update] [--no-service] [--no-reboot]
#
# What it does, in order (each step is a function below, run from main):
#   1. Raspberry Pi power: allow full USB current and the 5 A PSU limit, so
#      three cameras and a display do not brown out the board.
#   2. apt update/upgrade (skipped with --skip-update).
#   3. apt install of the runtime dependencies.
#   4. Free every camera from whatever holds it and disable the usual
#      camera-grabbing services (ZoneMinder, motion, mjpeg-streamer).
#   5. Put the target user in the video group.
#   6. Create the logs/ directory and a desktop shortcut.
#   7. Install the user systemd service via ./setup-service.sh
#      (skipped with --no-service; run that script alone to redo it).
#   8. Open camera 0 once as a smoke test.
#   9. Reboot in 5 seconds (skipped with --no-reboot).
#
# "Target user" is whoever invoked sudo; running this directly as root
# installs for root. The script edits /boot/firmware/config.txt and the Pi
# EEPROM; both edits are idempotent, so re-running is safe.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_TXT="/boot/firmware/config.txt"

# ---------------------------------------------------------------------------
# Arguments and checks
# ---------------------------------------------------------------------------

SKIP_UPDATE=false
INSTALL_SERVICE=true
DO_REBOOT=true

usage() {
  sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
  case "$arg" in
    --skip-update) SKIP_UPDATE=true ;;
    --no-service)  INSTALL_SERVICE=false ;;
    --no-reboot)   DO_REBOOT=false ;;
    -h|--help)     usage; exit 0 ;;
    *)             echo "Unknown option: $arg" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ "$EUID" -ne 0 ]]; then
  echo "This script must be run with sudo." >&2
  echo "Usage: sudo ./install.sh [--skip-update] [--no-service] [--no-reboot]" >&2
  exit 1
fi

TARGET_USER="${SUDO_USER:-$(id -un)}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

section() {
  echo
  echo "========================================"
  echo "$1"
  echo "========================================"
}

# Append LINE to FILE unless an identical line is already there.
ensure_line() {
  local file="$1" line="$2"
  if grep -qxF "$line" "$file"; then
    echo "$line already present in $file"
  else
    echo "$line" >> "$file"
    echo "Added $line to $file"
  fi
}

# Set KEY=VALUE in config.txt, replacing any existing KEY= line. When the
# file has an auto_initramfs line the setting is placed right after it, to
# stay in the [all] section that Raspberry Pi OS writes at the top; otherwise
# it goes at the end.
set_config_txt_key() {
  local key="$1" value="$2"
  sed -i "/^${key}=/d" "$CONFIG_TXT"
  if grep -q "^auto_initramfs" "$CONFIG_TXT"; then
    sed -i "/^auto_initramfs/ a ${key}=${value}" "$CONFIG_TXT"
  else
    echo "${key}=${value}" >> "$CONFIG_TXT"
  fi
  echo "Set ${key}=${value} in $CONFIG_TXT"
}

release_cameras() {
  for dev in /dev/video*; do
    [[ -e "$dev" ]] && fuser -k "$dev" 2>/dev/null || true
  done
  killall -9 zmc zma zoneminder motion 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

configure_power() {
  section "1) Configuring USB and PSU current"
  if [[ -f "$CONFIG_TXT" ]]; then
    set_config_txt_key usb_max_current_enable 1
    ensure_line "$CONFIG_TXT" "dtoverlay=vc4-kms-v3d,noaudio"
  else
    echo "Warning: $CONFIG_TXT not found; skipping usb_max_current_enable (not a Pi?)"
  fi

  # PSU_MAX_CURRENT=5000 tells Pi 5 firmware a 5 A supply is attached, which
  # lifts the USB current cap. Harmless on boards without the setting.
  if command -v rpi-eeprom-config >/dev/null 2>&1; then
    local tmp
    tmp="$(mktemp)"
    rpi-eeprom-config > "$tmp"
    if grep -q "^PSU_MAX_CURRENT=" "$tmp"; then
      sed -i "s/^PSU_MAX_CURRENT=.*/PSU_MAX_CURRENT=5000/" "$tmp"
    else
      echo "PSU_MAX_CURRENT=5000" >> "$tmp"
    fi
    rpi-eeprom-config --apply "$tmp"
    rm -f "$tmp"
    echo "EEPROM: PSU_MAX_CURRENT=5000"
  else
    echo "Warning: rpi-eeprom-config not available; skipping PSU_MAX_CURRENT"
  fi
}

update_system() {
  if [[ "$SKIP_UPDATE" == true ]]; then
    section "2) Skipping update (--skip-update)"
    return
  fi
  section "2) Updating system packages"
  apt update
  apt upgrade -y
}

install_packages() {
  section "3) Installing system dependencies"
  # `|| true`: a missing optional package on an unusual distro should not
  # abort the install; the smoke test at the end reports what actually works.
  apt install -y \
    python3 python3-pip \
    python3-pyqt6 python3-opencv python3-numpy \
    libgl1 libegl1 libxkbcommon0 libxkbcommon-x11-0 \
    libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-render-util0 libxcb-xinerama0 libxcb-xfixes0 \
    libqt6gui6 libqt6widgets6 \
    v4l-utils || true
}

clear_camera_holders() {
  section "4) Clearing camera devices"
  release_cameras
  for svc in zoneminder motion mjpeg-streamer; do
    if systemctl list-unit-files | grep -q "^$svc.service"; then
      echo "Disabling $svc..."
      systemctl stop "$svc" 2>/dev/null || true
      systemctl disable "$svc" 2>/dev/null || true
    fi
  done
}

add_user_to_video_group() {
  section "5) Adding $TARGET_USER to the video group"
  if id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx video; then
    echo "$TARGET_USER is already in the video group."
  else
    usermod -aG video "$TARGET_USER"
    echo "Added $TARGET_USER to the video group (takes effect at next login)."
  fi
}

create_logs_and_shortcut() {
  section "6) Creating logs directory and desktop shortcut"
  mkdir -p "$SCRIPT_DIR/logs"
  chown "$TARGET_USER:$TARGET_USER" "$SCRIPT_DIR/logs"

  local desktop_file="$TARGET_HOME/Desktop/CameraDashboard.desktop"
  mkdir -p "$(dirname "$desktop_file")"
  cat > "$desktop_file" <<EOF
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
  chown "$TARGET_USER:$TARGET_USER" "$desktop_file"
  chmod +x "$desktop_file"
  echo "Wrote $desktop_file"
}

install_service() {
  if [[ "$INSTALL_SERVICE" == false ]]; then
    section "7) Skipping systemd service (--no-service)"
    return
  fi
  section "7) Setting up the systemd user service"
  "$SCRIPT_DIR/setup-service.sh" --user "$TARGET_USER"
}

quick_test() {
  section "8) Quick test"
  release_cameras
  if timeout 10 python3 - <<'EOF'
import cv2
cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
if not cap.isOpened():
    print("Camera: failed to open")
else:
    ret, _ = cap.read()
    cap.release()
    print("Camera: OK" if ret else "Camera: opened but no frame")
EOF
  then
    echo "Quick test passed!"
  else
    echo "Quick test had issues (may be normal if no camera connected)"
  fi
}

finish() {
  section "Installation complete"
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
  if [[ "$DO_REBOOT" == true ]]; then
    echo "Rebooting in 5 seconds..."
    sleep 5
    reboot
  else
    echo "Reboot skipped (--no-reboot). Reboot before relying on the power settings."
  fi
}

# ---------------------------------------------------------------------------

main() {
  cd "$SCRIPT_DIR"
  configure_power
  update_system
  install_packages
  clear_camera_holders
  add_user_to_video_group
  create_logs_and_shortcut
  install_service
  quick_test
  finish
}

main
