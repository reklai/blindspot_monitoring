#!/usr/bin/env bash
#
# Install (or remove) the camera-dashboard user service.
#
# The dashboard runs as a *user* systemd unit so it can talk to the user's
# Wayland/X11 session. Linger is enabled so that unit starts at boot without
# anyone logging in. This script can be run two ways:
#
#   sudo ./setup-service.sh              # from install.sh, or by hand as root:
#                                        # installs for the invoking sudo user
#   ./setup-service.sh                   # as the target user, no root: installs
#                                        # for yourself (linger needs root and
#                                        # is skipped with a warning)
#
# Options:
#   --user NAME     install for NAME instead of $SUDO_USER (root only)
#   --remove        stop, disable and delete the unit
#   --print-unit    print the unit file that would be written, then exit
#
# The unit file content is the contract with the running system; change it
# here and nowhere else.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="camera-dashboard"

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

TARGET_USER=""
ACTION="install"

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)        TARGET_USER="${2:?--user needs a name}"; shift 2 ;;
    --remove)      ACTION="remove"; shift ;;
    --print-unit)  ACTION="print"; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "$TARGET_USER" ]]; then
  if [[ "$EUID" -eq 0 ]]; then
    TARGET_USER="${SUDO_USER:-root}"
  else
    TARGET_USER="$(id -un)"
  fi
elif [[ "$EUID" -ne 0 && "$TARGET_USER" != "$(id -un)" ]]; then
  echo "Installing for another user needs root: sudo ./setup-service.sh --user $TARGET_USER" >&2
  exit 1
fi

if ! id "$TARGET_USER" >/dev/null 2>&1; then
  echo "No such user: $TARGET_USER" >&2
  exit 1
fi

TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
UNIT_DIR="$TARGET_HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/$SERVICE_NAME.service"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Run `systemctl --user ...` as the target user. As root we cannot simply
# call systemctl --user (it would address root's own manager), so we go
# through the user's manager via --machine, and fall back to su with the
# runtime dir set for systemd versions that lack --machine=user@ support.
user_systemctl() {
  if [[ "$EUID" -ne 0 ]]; then
    systemctl --user "$@"
  else
    systemctl --machine="$TARGET_USER@" --user "$@" 2>/dev/null ||
      su - "$TARGET_USER" -c "XDG_RUNTIME_DIR=/run/user/$TARGET_UID systemctl --user $*"
  fi
}

# The unit, with the paths of this checkout and this user baked in.
#
# - ExecStartPre 1 evicts anything holding a camera (a crashed previous
#   instance, motion, ZoneMinder) so the dashboard's own discovery finds
#   the devices free.
# - ExecStartPre 2 waits up to 30 s for a Wayland or X11 socket so the
#   unit does not spin through its start limit before the desktop is up;
#   it exits 0 either way and lets the app itself report a missing display.
# - QT_QPA_PLATFORM lists wayland first with xcb as the fallback.
# - Restart=always with RestartSec=5 is the crash-recovery policy: the app
#   never tries to survive a fatal error, it exits and comes back in 5 s.
render_unit() {
  cat <<EOF
[Unit]
Description=Camera Dashboard
After=default.target
Wants=default.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStartPre=/bin/bash -c 'for dev in /dev/video*; do fuser -k "\$dev" 2>/dev/null || true; done; killall -9 zmc zma zoneminder motion 2>/dev/null || true'
ExecStartPre=/bin/bash -c 'for i in \$(seq 1 30); do [ -e /run/user/$TARGET_UID/wayland-0 ] && exit 0; [ -e /tmp/.X11-unix/X0 ] && exit 0; sleep 1; done; exit 0'
ExecStart=python3 $SCRIPT_DIR/main.py
Environment=DISPLAY=:0
Environment=WAYLAND_DISPLAY=wayland-0
Environment=XDG_RUNTIME_DIR=/run/user/$TARGET_UID
Environment="QT_QPA_PLATFORM=wayland;xcb"
KillSignal=SIGTERM
TimeoutStopSec=10
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
}

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

install_service() {
  echo "Installing $SERVICE_NAME.service for $TARGET_USER"
  mkdir -p "$UNIT_DIR"
  render_unit > "$UNIT_PATH"
  if [[ "$EUID" -eq 0 ]]; then
    chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/.config/systemd"
  fi
  echo "Wrote $UNIT_PATH"

  # Linger keeps the user's systemd manager (and so this unit) running from
  # boot, with no login. Root only.
  if [[ "$EUID" -eq 0 ]]; then
    loginctl enable-linger "$TARGET_USER" 2>/dev/null ||
      echo "Warning: could not enable linger; the service will start at login instead of boot"
  elif ! loginctl show-user "$TARGET_USER" 2>/dev/null | grep -q '^Linger=yes'; then
    echo "Warning: linger is not enabled for $TARGET_USER; run: sudo loginctl enable-linger $TARGET_USER"
  fi

  user_systemctl daemon-reload 2>/dev/null ||
    echo "Note: user systemd is not running yet; the unit is picked up at next login/boot"
  user_systemctl enable "$SERVICE_NAME.service" 2>/dev/null ||
    echo "Note: could not enable the unit now; it will be enabled at next login/boot"

  cat <<EOF

Service installed. Manage it as $TARGET_USER with:
  systemctl --user start $SERVICE_NAME
  systemctl --user status $SERVICE_NAME
  journalctl --user -u $SERVICE_NAME -f
EOF
}

remove_service() {
  echo "Removing $SERVICE_NAME.service for $TARGET_USER"
  user_systemctl stop "$SERVICE_NAME.service" 2>/dev/null || true
  user_systemctl disable "$SERVICE_NAME.service" 2>/dev/null || true
  if [[ -f "$UNIT_PATH" ]]; then
    rm -f "$UNIT_PATH"
    echo "Deleted $UNIT_PATH"
  else
    echo "No unit file at $UNIT_PATH"
  fi
  user_systemctl daemon-reload 2>/dev/null || true
  echo "Linger is left as it was; to undo it: sudo loginctl disable-linger $TARGET_USER"
}

case "$ACTION" in
  print)   render_unit ;;
  install) install_service ;;
  remove)  remove_service ;;
esac
