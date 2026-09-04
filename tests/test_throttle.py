"""Tests for core/throttle.py - deadline-based frame rate limiting."""

import random

import pytest

from core.throttle import FrameThrottle


def _emitted_fps(throttle: FrameThrottle, source_fps: float, seconds: float, jitter: float = 0.0, seed: int = 1) -> float:
    """Feed frames at ``source_fps`` (with +/- jitter seconds) and return the accepted rate."""
    rng = random.Random(seed)
    period = 1.0 / source_fps
    count = int(source_fps * seconds)
    accepted = 0
    for i in range(count):
        t = i * period + (rng.uniform(-jitter, jitter) if jitter else 0.0)
        if throttle.accept(t):
            accepted += 1
    return accepted / seconds


class TestFrameThrottle:
    def test_first_frame_is_always_accepted(self):
        assert FrameThrottle(25).accept(123.456) is True

    def test_interval_and_fps_roundtrip(self):
        t = FrameThrottle(20)
        assert t.interval == pytest.approx(0.05)
        assert t.fps == pytest.approx(20)
        t.set_fps(10)
        assert t.interval == pytest.approx(0.1)

    def test_sub_one_fps_is_clamped(self):
        assert FrameThrottle(0.1).interval == pytest.approx(1.0)
        t = FrameThrottle(30)
        t.set_fps(0)
        assert t.interval == pytest.approx(1.0)

    def test_source_at_target_rate_keeps_every_frame_despite_jitter(self):
        # This is the case the phase-resetting throttle got wrong: a 25 FPS
        # camera feeding a 25 FPS throttle must not lose frames to jitter.
        fps = _emitted_fps(FrameThrottle(25), source_fps=25, seconds=20, jitter=0.002)
        assert fps == pytest.approx(25, abs=0.2)

    def test_source_slightly_faster_than_target_is_not_halved(self):
        fps = _emitted_fps(FrameThrottle(25), source_fps=25.05, seconds=40)
        assert fps == pytest.approx(25, abs=0.3)

    def test_source_at_double_rate_is_halved(self):
        fps = _emitted_fps(FrameThrottle(25), source_fps=50, seconds=20, jitter=0.001)
        assert fps == pytest.approx(25, abs=0.2)

    def test_30_to_25_keeps_five_of_six(self):
        fps = _emitted_fps(FrameThrottle(25), source_fps=30, seconds=30, jitter=0.001)
        assert fps == pytest.approx(25, abs=0.3)

    def test_long_run_rate_never_exceeds_target(self):
        for source in (25, 26, 30, 45, 60, 120):
            fps = _emitted_fps(FrameThrottle(20), source_fps=source, seconds=30, jitter=0.003)
            assert fps <= 20.05, f"source={source} emitted {fps}"

    def test_stall_does_not_burst(self):
        t = FrameThrottle(10)
        assert t.accept(0.0)
        # Source goes quiet for a second, then delivers a burst at 100 Hz.
        accepted = [t.accept(1.0 + i * 0.01) for i in range(10)]
        # Without re-anchoring, the deadline would still be at 0.1s and every
        # burst frame would be accepted to "catch up". Only the first frame
        # (and at most one more inside the early tolerance) may pass.
        assert accepted[0] is True
        assert not any(accepted[1:7])
        assert sum(accepted) <= 2

    def test_reset_accepts_immediately(self):
        t = FrameThrottle(10)
        assert t.accept(0.0)
        assert t.accept(0.01) is False
        t.reset()
        assert t.accept(0.02) is True

    def test_lowering_fps_mid_stream_takes_effect(self):
        t = FrameThrottle(20)
        # Prime the schedule at 20 FPS.
        for i in range(10):
            t.accept(i * 0.05)
        t.set_fps(5)
        accepted = sum(1 for i in range(10, 110) if t.accept(i * 0.05))
        # 100 frames over 5 seconds at 5 FPS -> 25 accepted (allow one for phase).
        assert 24 <= accepted <= 26
