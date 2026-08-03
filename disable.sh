#!/usr/bin/env bash
set -euo pipefail

# Disable onboard Wi-Fi and Bluetooth on Raspberry Pi (device-tree overlays).
# Takes effect after reboot.
#
#   chmod +x disable.sh
#   sudo ./disable.sh

if [[ "$EUID" -ne 0 ]]; then
  echo "This script must be run with sudo."
  echo "Usage: sudo ./disable.sh"
  exit 1
fi

CONFIG_TXT="/boot/firmware/config.txt"
if [[ ! -f "$CONFIG_TXT" ]]; then
  echo "Error: $CONFIG_TXT not found (is this a Raspberry Pi with firmware on /boot/firmware?)"
  exit 1
fi

if ! grep -q "^dtoverlay=disable-wifi$" "$CONFIG_TXT"; then
  echo "dtoverlay=disable-wifi" >> "$CONFIG_TXT"
  echo "Added dtoverlay=disable-wifi"
else
  echo "dtoverlay=disable-wifi already present"
fi

if ! grep -q "^dtoverlay=disable-bt$" "$CONFIG_TXT"; then
  echo "dtoverlay=disable-bt" >> "$CONFIG_TXT"
  echo "Added dtoverlay=disable-bt"
else
  echo "dtoverlay=disable-bt already present"
fi

echo
echo "Onboard Wi-Fi and Bluetooth will be disabled after reboot."
echo "Reboot when ready: sudo reboot"
