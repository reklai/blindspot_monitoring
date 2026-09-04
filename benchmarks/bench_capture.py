#!/usr/bin/env python3
"""
CPU cost of the capture loop against a real camera.

Runs a CaptureWorker for a few seconds at several emit rates and reports how
many frames were emitted and how much process CPU time the loop consumed.
The "unthrottled" row disables the throttle so every grabbed frame is also
retrieved; the difference to the throttled rows is what throttle-before-
retrieve saves (the decode on V4L2 MJPG, the copy on GStreamer).

    python3 benchmarks/bench_capture.py --device 0 --seconds 6
    python3 benchmarks/bench_capture.py --device 0 --no-gstreamer

Needs a camera nobody else is using. Uses the same config.ini as the app for
capture size unless overridden.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6 import QtCore, QtWidgets  # noqa: E402

from core import config  # noqa: E402
from core.camera import CaptureWorker  # noqa: E402
from core.throttle import FrameThrottle  # noqa: E402


class _AlwaysAccept(FrameThrottle):
    """Throttle that never drops, so retrieve() runs for every grab()."""

    def accept(self, now: float) -> bool:
        return True


def run_once(device: int, seconds: float, target_fps: float | None, width: int, height: int, unthrottled: bool):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    worker = CaptureWorker(device, target_fps=target_fps, capture_width=width, capture_height=height)
    if unthrottled:
        worker._throttle = _AlwaysAccept(1000.0)

    emitted = {"n": 0, "first": None}

    def on_frame(_frame):
        emitted["n"] += 1
        if emitted["first"] is None:
            emitted["first"] = time.perf_counter()

    worker.frame_ready.connect(on_frame, type=QtCore.Qt.ConnectionType.DirectConnection)
    worker.start()

    # Let the device open and settle before measuring.
    deadline = time.perf_counter() + 5.0
    while emitted["first"] is None and time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.01)
    if emitted["first"] is None:
        worker.stop()
        return None

    emitted["n"] = 0
    grabs0 = worker.grab_count
    cpu0 = time.process_time()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        app.processEvents()
        time.sleep(0.005)
    cpu = time.process_time() - cpu0
    wall = time.perf_counter() - t0
    grabbed = worker.grab_count - grabs0
    worker.stop()
    return grabbed / wall, emitted["n"] / wall, cpu / wall * 100.0, worker.get_fourcc()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--size", default=None, help="capture size WxH (default from config.ini)")
    parser.add_argument("--no-gstreamer", action="store_true", help="force the V4L2 path")
    parser.add_argument("--rates", default="unthrottled,25,20,10", help="comma list of emit rates to test")
    args = parser.parse_args()

    config.apply_config(config.load_config())
    if args.no_gstreamer:
        config.USE_GSTREAMER = False
    if args.size:
        width, height = (int(v) for v in args.size.lower().split("x"))
    else:
        width, height = config.PROFILE_CAPTURE_WIDTH, config.PROFILE_CAPTURE_HEIGHT

    print(f"device /dev/video{args.device}, {width}x{height}, {args.seconds:.0f}s per row")
    print(f"{'emit target':14s} {'source fps':>11s} {'emitted fps':>12s} {'process cpu%':>13s}  fourcc")
    for rate in args.rates.split(","):
        rate = rate.strip()
        unthrottled = rate == "unthrottled"
        target = None if unthrottled else float(rate)
        result = run_once(args.device, args.seconds, target, width, height, unthrottled)
        if result is None:
            print(f"{rate:14s} {'no frames':>11s}")
            continue
        source_fps, fps, cpu_pct, fourcc = result
        print(f"{rate:14s} {source_fps:11.1f} {fps:12.1f} {cpu_pct:13.1f}  {fourcc}")


if __name__ == "__main__":
    main()
