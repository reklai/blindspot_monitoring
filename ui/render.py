"""
Frame styling: brightness and night mode, from numpy frame to QImage.

``FrameStyler`` is the only place pixels are touched on the UI thread, so
its cost is paid once per rendered frame per tile. Two rules keep it cheap:

* Every adjustment is a 256-entry lookup table. Brightness is one LUT,
  night mode is a grayscale conversion followed by a LUT, and when both are
  on the two tables are composed ahead of time so the frame is still
  touched once.
* Night mode never builds a 3-channel red image. The gray plane is handed
  to Qt as an 8-bit indexed image whose palette *is* the red tint, so the
  tint costs nothing per pixel and a third of the bytes cross into Qt.

The input frame is never modified. Earlier code applied brightness in place
on the shared latest frame, which doubled it every time the same frame was
re-rendered (a tile resize, a fullscreen toggle).

The returned ``QImage`` borrows memory from either the input frame or the
styler's scratch buffers; convert it to a ``QPixmap`` before the next call
or before the frame is released.
"""

from __future__ import annotations

from typing import Optional

import cv2
import numpy as np
from numpy.typing import NDArray
from PyQt6 import QtGui

Frame = NDArray[np.uint8]

#: Gain applied to luminance in night mode before the red tint.
NIGHT_GAIN = 1.6
#: Brightness multiplier bounds; the settings tile's 15% preset lands on 0.5.
BRIGHTNESS_MIN = 0.5
BRIGHTNESS_MAX = 3.0


def brightness_lut(multiplier: float) -> NDArray[np.uint8]:
    """Lookup table scaling every level by ``multiplier``, clipped to 8 bits."""
    levels = np.arange(256, dtype=np.float32) * float(multiplier)
    return np.clip(levels, 0, 255).astype(np.uint8)


_NIGHT_LUT = brightness_lut(NIGHT_GAIN)
_IDENTITY_LUT = np.arange(256, dtype=np.uint8)


def _qimage(buffer: Frame, fmt: QtGui.QImage.Format) -> QtGui.QImage:
    height, width = buffer.shape[:2]
    channels = 1 if buffer.ndim == 2 else buffer.shape[2]
    return QtGui.QImage(buffer.data, width, height, width * channels, fmt)


class FrameStyler:
    """Applies the tile's night-mode and brightness settings to frames."""

    def __init__(self) -> None:
        self._night_mode = False
        self._brightness = 1.0
        self._day_lut: NDArray[np.uint8] = _IDENTITY_LUT
        self._night_palette: list[int] = []
        self._gray: Optional[Frame] = None
        self._day_out: Optional[Frame] = None
        self._rebuild_tables()

    @property
    def night_mode(self) -> bool:
        return self._night_mode

    @property
    def brightness(self) -> float:
        return self._brightness

    def set_night_mode(self, enabled: bool) -> None:
        self._night_mode = bool(enabled)

    def set_brightness(self, multiplier: float) -> None:
        """Set the brightness multiplier, clamped to the supported range."""
        self._brightness = max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, float(multiplier)))
        self._rebuild_tables()

    def _rebuild_tables(self) -> None:
        if self._brightness == 1.0:
            self._day_lut = _IDENTITY_LUT
            night = _NIGHT_LUT
        else:
            self._day_lut = brightness_lut(self._brightness)
            # Brightness runs after the night gain, exactly as two passes would.
            night = self._day_lut[_NIGHT_LUT]
        self._night_palette = [QtGui.qRgb(int(level), 0, 0) for level in night]

    def to_qimage(self, frame: Frame) -> QtGui.QImage:
        """Style ``frame`` (BGR or grayscale, uint8) and wrap it as a QImage."""
        if self._night_mode:
            return self._night_image(frame)
        return self._day_image(frame)

    def _day_image(self, frame: Frame) -> QtGui.QImage:
        if self._brightness != 1.0:
            if self._day_out is None or self._day_out.shape != frame.shape:
                self._day_out = np.empty_like(frame, order="C")
            cv2.LUT(frame, self._day_lut, dst=self._day_out)
            frame = self._day_out
        elif not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)
        fmt = (
            QtGui.QImage.Format.Format_Grayscale8
            if frame.ndim == 2
            else QtGui.QImage.Format.Format_BGR888
        )
        return _qimage(frame, fmt)

    def _night_image(self, frame: Frame) -> QtGui.QImage:
        if frame.ndim == 2:
            gray = frame if frame.flags["C_CONTIGUOUS"] else np.ascontiguousarray(frame)
        else:
            shape = frame.shape[:2]
            if self._gray is None or self._gray.shape != shape:
                self._gray = np.empty(shape, dtype=np.uint8)
            cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY, dst=self._gray)
            gray = self._gray
        image = _qimage(gray, QtGui.QImage.Format.Format_Indexed8)
        image.setColorTable(self._night_palette)
        return image
