#!/usr/bin/env bash
#
# Disable onboard Wi-Fi and Bluetooth on a Raspberry Pi.
#
#   sudo ./disable.sh
#
# Adds the device-tree overlays that leave both radios unpowered from boot.
# On a vehicle unit that is wired or offline they only draw power and add
# heat; on a unit that needs Wi-Fi for maintenance, do not run this. Takes
# effect after a reboot, which this script does not do. Safe to re-run: a
# line that is already present is not added twice.

set -euo pipefail

CONFIG_TXT="/boot/firmware/config.txt"

if [[ "$EUID" -ne 0 ]]; then
  echo "This script must be run with sudo." >&2
  echo "Usage: sudo ./disable.sh" >&2
  exit 1
fi

if [[ ! -f "$CONFIG_TXT" ]]; then
  echo "Error: $CONFIG_TXT not found (is this a Raspberry Pi with firmware on /boot/firmware?)" >&2
  exit 1
fi

# Append LINE to FILE unless an identical line is already there.
ensure_line() {
  local file="$1" line="$2"
  if grep -qxF "$line" "$file"; then
    echo "$line already present"
  else
    echo "$line" >> "$file"
    echo "Added $line"
  fi
}

ensure_line "$CONFIG_TXT" "dtoverlay=disable-wifi"
ensure_line "$CONFIG_TXT" "dtoverlay=disable-bt"

echo
echo "Onboard Wi-Fi and Bluetooth will be disabled after reboot."
echo "Reboot when ready: sudo reboot"
