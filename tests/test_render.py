"""Tests for ui/render.py - brightness and night-mode styling."""

import numpy as np
import pytest
from PyQt6 import QtGui

from ui.render import BRIGHTNESS_MAX, BRIGHTNESS_MIN, NIGHT_GAIN, FrameStyler, brightness_lut


def _rgb_at(image: QtGui.QImage, x: int = 0, y: int = 0) -> tuple[int, int, int]:
    c = image.pixelColor(x, y)
    return c.red(), c.green(), c.blue()


def _frame(b: int, g: int, r: int, h: int = 4, w: int = 6) -> np.ndarray:
    f = np.empty((h, w, 3), np.uint8)
    f[:, :, 0], f[:, :, 1], f[:, :, 2] = b, g, r
    return f


class TestBrightnessLut:
    def test_identity(self):
        assert np.array_equal(brightness_lut(1.0), np.arange(256, dtype=np.uint8))

    def test_darken_and_brighten_clip(self):
        assert brightness_lut(0.5)[200] == 100
        assert brightness_lut(2.0)[200] == 255
        assert brightness_lut(1.5)[100] == 150


@pytest.mark.usefixtures("qapp")
class TestFrameStyler:
    def test_defaults_pass_frame_through(self):
        s = FrameStyler()
        frame = _frame(10, 20, 30)
        img = s.to_qimage(frame)
        assert img.format() == QtGui.QImage.Format.Format_BGR888
        assert _rgb_at(img) == (30, 20, 10)

    def test_brightness_is_clamped(self):
        s = FrameStyler()
        s.set_brightness(0.15)
        assert s.brightness == BRIGHTNESS_MIN
        s.set_brightness(10)
        assert s.brightness == BRIGHTNESS_MAX

    def test_brightness_scales_all_channels_without_touching_input(self):
        s = FrameStyler()
        s.set_brightness(1.5)
        frame = _frame(100, 120, 200)
        img = s.to_qimage(frame)
        assert _rgb_at(img) == (255, 180, 150)
        assert (frame[0, 0] == (100, 120, 200)).all(), "input frame must not be modified"

    def test_rerendering_same_frame_is_idempotent(self):
        s = FrameStyler()
        s.set_brightness(1.5)
        frame = _frame(100, 100, 100)
        first = _rgb_at(s.to_qimage(frame))
        second = _rgb_at(s.to_qimage(frame))
        assert first == second == (150, 150, 150)

    def test_brightness_on_grayscale_input(self):
        s = FrameStyler()
        s.set_brightness(0.5)
        frame = np.full((4, 6), 200, np.uint8)
        img = s.to_qimage(frame)
        assert img.format() == QtGui.QImage.Format.Format_Grayscale8
        assert _rgb_at(img) == (100, 100, 100)

    def test_night_mode_is_red_tinted_luminance_with_gain(self):
        s = FrameStyler()
        s.set_night_mode(True)
        frame = _frame(50, 50, 50)  # gray 50 -> 80 after the 1.6x gain
        img = s.to_qimage(frame)
        assert img.format() == QtGui.QImage.Format.Format_Indexed8
        assert _rgb_at(img) == (int(50 * NIGHT_GAIN), 0, 0)

    def test_night_mode_composes_brightness_after_gain(self):
        s = FrameStyler()
        s.set_night_mode(True)
        s.set_brightness(2.0)
        frame = _frame(100, 100, 100)  # 100 * 1.6 = 160, * 2.0 = clipped to 255
        assert _rgb_at(s.to_qimage(frame)) == (255, 0, 0)
        s.set_brightness(0.5)
        assert _rgb_at(s.to_qimage(frame)) == (80, 0, 0)

    def test_night_mode_matches_explicit_two_pass_reference(self):
        rng = np.random.default_rng(3)
        frame = rng.integers(0, 256, (8, 8, 3), dtype=np.uint8)
        s = FrameStyler()
        s.set_night_mode(True)
        s.set_brightness(1.5)
        img = s.to_qimage(frame)

        import cv2
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        reference = brightness_lut(1.5)[brightness_lut(NIGHT_GAIN)[gray]]
        for y in range(8):
            for x in range(8):
                assert _rgb_at(img, x, y) == (int(reference[y, x]), 0, 0)

    def test_night_mode_on_grayscale_input(self):
        s = FrameStyler()
        s.set_night_mode(True)
        frame = np.full((4, 6), 100, np.uint8)
        assert _rgb_at(s.to_qimage(frame)) == (160, 0, 0)

    def test_scratch_buffers_follow_resolution_changes(self):
        s = FrameStyler()
        s.set_brightness(1.5)
        s.set_night_mode(True)
        assert _rgb_at(s.to_qimage(_frame(10, 10, 10, h=4, w=4))) == (24, 0, 0)
        assert _rgb_at(s.to_qimage(_frame(20, 20, 20, h=8, w=6))) == (48, 0, 0)
        s.set_night_mode(False)
        assert _rgb_at(s.to_qimage(_frame(10, 10, 10, h=4, w=4))) == (15, 15, 15)
        assert _rgb_at(s.to_qimage(_frame(20, 20, 20, h=8, w=6))) == (30, 30, 30)
