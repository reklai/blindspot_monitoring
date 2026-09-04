"""
Bounded restart policy for stalled capture workers.

A camera that stops delivering frames gets its worker restarted, but only
within a budget: restarts are spaced at least ``cooldown_sec`` apart and at
most ``max_per_window`` may happen in any ``window_sec``. Once the budget is
spent the policy refuses restarts for an extended cooldown of twice the
window. After that the budget is cleared and the next request is granted, so
a camera is never given up on permanently; the dashboard may instead choose
to detach it (see ``is_beyond_extended_cooldown``) and free the slot.

The policy is a plain object with explicit clocks so it can be tested without
sleeping. Callers pass ``time.time()`` (or any monotonic seconds value, as
long as they are consistent).
"""

from __future__ import annotations

from collections import deque
from enum import Enum, auto


class RestartVerdict(Enum):
    """Outcome of ``RestartBudget.request``."""

    GRANTED = auto()
    """Restart now; the budget has been charged."""

    RECOVERED = auto()
    """Restart now; the extended cooldown has cleared an exhausted budget."""

    COOLING_DOWN = auto()
    """Too soon since the last restart."""

    EXHAUSTED = auto()
    """Budget spent; wait for the extended cooldown."""


class RestartBudget:
    """Rate limiter for worker restarts, see module docstring."""

    def __init__(
        self,
        cooldown_sec: float,
        window_sec: float,
        max_per_window: int,
    ) -> None:
        self.cooldown_sec = float(cooldown_sec)
        self.window_sec = float(window_sec)
        self.max_per_window = max(1, int(max_per_window))
        self._events: deque[float] = deque(maxlen=self.max_per_window * 2)
        self._last_restart_ts: float | None = None
        self._exhausted = False

    @property
    def extended_cooldown_sec(self) -> float:
        """How long an exhausted budget refuses restarts."""
        return self.window_sec * 2

    @property
    def last_restart_ts(self) -> float | None:
        """Timestamp of the most recent granted restart, or None if never."""
        return self._last_restart_ts

    @property
    def exhausted(self) -> bool:
        """True from the moment the budget is refused until it recovers."""
        return self._exhausted

    def reset(self) -> None:
        """Forget all history, e.g. when a different camera is attached."""
        self._events.clear()
        self._last_restart_ts = None
        self._exhausted = False

    def is_beyond_extended_cooldown(self, now: float) -> bool:
        """True if the budget is exhausted and the extended cooldown has passed.

        This is the signal the dashboard uses to detach a camera whose worker
        has not recovered on its own.
        """
        return self._exhausted and self._since_last_restart(now) >= self.extended_cooldown_sec

    def _since_last_restart(self, now: float) -> float:
        if self._last_restart_ts is None:
            return float("inf")
        return now - self._last_restart_ts

    def request(self, now: float) -> RestartVerdict:
        """Ask to restart at time ``now``; charges the budget if granted."""
        since_last = self._since_last_restart(now)
        if since_last < self.cooldown_sec:
            return RestartVerdict.COOLING_DOWN

        if self._exhausted:
            if since_last < self.extended_cooldown_sec:
                return RestartVerdict.EXHAUSTED
            self._events.clear()
            self._exhausted = False
            self._events.append(now)
            self._last_restart_ts = now
            return RestartVerdict.RECOVERED

        recent = sum(1 for t in self._events if (now - t) <= self.window_sec)
        if recent >= self.max_per_window:
            self._exhausted = True
            return RestartVerdict.EXHAUSTED

        self._events.append(now)
        self._last_restart_ts = now
        return RestartVerdict.GRANTED
