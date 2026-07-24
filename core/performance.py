"""Best-effort CPU load and temperature signals for dynamic FPS control."""

from __future__ import annotations

import os
from typing import Optional

from core import config


def read_cpu_load_ratio() -> Optional[float]:
    """Return the one-minute load per CPU, capped at 1.0 when available."""
    try:
        load1, _, _ = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        return min(1.0, load1 / cpu_count)
    except Exception:
        return None


def read_cpu_temp_c() -> Optional[float]:
    """Return the first readable Linux thermal sensor value in Celsius.

    Kernel sensors commonly report millidegrees, so values above 1000 are
    converted before returning. Missing or malformed sensor files are skipped.
    """
    paths = [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/class/hwmon/hwmon0/temp1_input",
    ]
    for p in paths:
        try:
            if os.path.exists(p):
                with open(p, "r") as f:
                    raw = f.read().strip()
                if raw:
                    val = float(raw)
                    if val > 1000:
                        val = val / 1000.0
                    return val
        except Exception:
            continue
    return None


def is_system_stressed() -> tuple[bool, Optional[float], Optional[float]]:
    """Return whether either available CPU metric meets its stress threshold.

    The remaining tuple values are ``(load_ratio, temperature_c)``; either may
    be ``None`` when the host does not expose that metric.
    """
    load_ratio = read_cpu_load_ratio()
    temp_c = read_cpu_temp_c()

    stressed = False
    if load_ratio is not None and load_ratio >= config.CPU_LOAD_THRESHOLD:
        stressed = True
    if temp_c is not None and temp_c >= config.CPU_TEMP_THRESHOLD_C:
        stressed = True

    return stressed, load_ratio, temp_c
