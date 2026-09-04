"""
System stress inputs for the dynamic-FPS controller.

Two signals, either of which counts as stress: the 1-minute load average
normalised by core count, and the CPU temperature. Both are cheap reads
(one libc call, one sysfs file) and run every PERF_CHECK_INTERVAL_MS on the
UI thread. Note the load average's one-minute time constant: it lags a
burst of work by tens of seconds, which is why the controller also needs
STRESS_HOLD_COUNT consecutive samples before acting. See
docs/performance-avenues.md for the case for a faster signal.
"""

from __future__ import annotations

import os
from typing import Optional

from core import config


def read_cpu_load_ratio() -> Optional[float]:
    """Read 1-minute load average normalized to CPU count."""
    try:
        load1, _, _ = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        return min(1.0, load1 / cpu_count)
    except Exception:
        return None


def read_cpu_temp_c() -> Optional[float]:
    """Read CPU temperature in Celsius if the system exposes it."""
    # thermal_zone0 is the SoC sensor on every Raspberry Pi; the hwmon path
    # covers generic x86 boards used for bench testing. Both report
    # millidegrees, hence the /1000 below (guarded, since a few drivers
    # report whole degrees).
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
    """
    Check CPU load or temperature thresholds.
    Returns: (stressed: bool, load_ratio: float|None, temp_c: float|None)
    """
    load_ratio = read_cpu_load_ratio()
    temp_c = read_cpu_temp_c()

    stressed = False
    if load_ratio is not None and load_ratio >= config.CPU_LOAD_THRESHOLD:
        stressed = True
    if temp_c is not None and temp_c >= config.CPU_TEMP_THRESHOLD_C:
        stressed = True

    return stressed, load_ratio, temp_c
