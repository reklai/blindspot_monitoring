#!/usr/bin/env python3
"""
Per-frame cost of the UI render path, stage by stage.

Run this on the target machine to see what one rendered frame costs and how
much of a core the dashboard spends at a given tile count and UI rate:

    QT_QPA_PLATFORM=offscreen python3 benchmarks/bench_render.py
    python3 benchmarks/bench_render.py --tiles 3 --ui-fps 20 --size 640x480

Stages measured, each in isolation on a random 8-bit BGR frame:

  convert      numpy -> QImage -> QPixmap, no styling (the floor)
  brightness   one 3-channel LUT into a scratch buffer, then convert
  night        BGR -> gray, indexed QImage with red palette, then convert
  night+bright same, with the composed table
  scale        draw the pixmap into a tile-sized pixmap (what _present does)

The numbers are wall-clock per call on an otherwise idle process. They are
not a substitute for measuring the running app, but they rank the stages and
show how far each is from the floor.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np  # noqa: E402
from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402

from ui.render import FrameStyler  # noqa: E402


def timeit(fn, iterations: int) -> float:
    fn()  # warm up
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    return (time.perf_counter() - start) / iterations * 1000.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--size", default="640x480", help="capture size WxH (default 640x480)")
    parser.add_argument("--tile", default="640x360", help="tile size WxH the frame is scaled to")
    parser.add_argument("--tiles", type=int, default=3, help="camera tiles on screen")
    parser.add_argument("--ui-fps", type=int, default=20, help="render rate per tile")
    parser.add_argument("--iterations", type=int, default=300)
    args = parser.parse_args()

    width, height = (int(v) for v in args.size.lower().split("x"))
    tile_w, tile_h = (int(v) for v in args.tile.lower().split("x"))

    # Keep a reference: QPixmap needs a live QApplication for the whole run.
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    assert app is not None
    frame = np.random.default_rng(0).integers(0, 256, (height, width, 3), dtype=np.uint8)
    pixmap = QtGui.QPixmap()

    def styled(night: bool, brightness: float):
        styler = FrameStyler()
        styler.set_night_mode(night)
        styler.set_brightness(brightness)

        def run():
            pixmap.convertFromImage(styler.to_qimage(frame))

        return run

    scaled = QtGui.QPixmap(QtCore.QSize(tile_w, tile_h))
    styled(False, 1.0)()  # populate pixmap for the scale stage

    def scale():
        scaled.fill(QtCore.Qt.GlobalColor.black)
        painter = QtGui.QPainter(scaled)
        painter.drawPixmap(QtCore.QRect(0, 0, tile_w, tile_h), pixmap)
        painter.end()

    stages = [
        ("convert", styled(False, 1.0)),
        ("brightness 1.5", styled(False, 1.5)),
        ("night", styled(True, 1.0)),
        ("night + brightness 1.5", styled(True, 1.5)),
        ("scale to tile", scale),
    ]

    print(f"frame {width}x{height} -> tile {tile_w}x{tile_h}, {args.iterations} iterations each")
    heading = f"core% at {args.tiles} tiles x {args.ui_fps} fps"
    print(f"{'stage':26s} {'ms/frame':>9s}   {heading:>26s}")
    for name, fn in stages:
        ms = timeit(fn, args.iterations)
        core_pct = ms * args.tiles * args.ui_fps / 10.0  # ms/frame * frames/s / 1000 * 100
        print(f"{name:26s} {ms:9.3f}   {core_pct:26.1f}")


if __name__ == "__main__":
    main()
