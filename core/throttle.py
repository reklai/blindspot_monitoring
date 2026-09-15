"""
Frame-rate limiter for the capture thread.

The camera delivers frames at its own cadence; the dashboard only wants
``target_fps`` of them. Deciding which frames to keep sounds trivial but the
obvious rule, "emit if at least one interval passed since the last emit",
aliases badly when the camera runs at (or a hair above) the target rate:
each frame arrives a few hundred microseconds early, is rejected, and the
next one is accepted, halving the effective rate.

``FrameThrottle`` instead tracks an absolute deadline that advances by whole
intervals, and accepts a frame that lands slightly early. Long-run output can
never exceed the target because the deadline only moves forward by one
interval per accepted frame; a stalled source re-anchors the deadline so it
does not burst to catch up.

Not thread-safe on its own. ``CaptureWorker`` serialises access with a lock
because the UI thread changes the target rate while the capture thread
consumes frames.
"""

from __future__ import annotations


class FrameThrottle:
    """Deadline-based rate limiter for frame emission."""

    # Fraction of one interval by which a frame may arrive early and still be
    # taken. Large enough to absorb USB timing jitter, small enough that a
    # source at 1.25x the target rate still gets filtered.
    EARLY_TOLERANCE = 0.25

    __slots__ = ("_interval", "_next_due")

    def __init__(self, fps: float) -> None:
        self._interval = 1.0 / max(1.0, float(fps))
        self._next_due: float | None = None

    @property
    def interval(self) -> float:
        """Seconds between accepted frames at the current target."""
        return self._interval

    @property
    def fps(self) -> float:
        return 1.0 / self._interval

    def set_fps(self, fps: float) -> None:
        """Change the target rate. Rates below 1 FPS are clamped to 1."""
        self._interval = 1.0 / max(1.0, float(fps))

    def reset(self) -> None:
        """Forget the schedule so the next frame is accepted immediately."""
        self._next_due = None

    def accept(self, now: float) -> bool:
        """Return True if the frame arriving at ``now`` should be emitted.

        Accepting advances the internal deadline, so call this once per frame.
        """
        interval = self._interval
        due = self._next_due
        if due is not None and now < due - interval * self.EARLY_TOLERANCE:
            return False

        if due is None:
            self._next_due = now + interval
        else:
            due += interval
            # The source stalled for more than an interval: restart the
            # schedule from this frame instead of accepting a burst.
            self._next_due = due if due > now else now + interval
        return True
