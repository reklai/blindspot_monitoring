"""Tests for core/recovery.py - bounded restart budget."""

from core.recovery import RestartBudget, RestartVerdict


def make_budget() -> RestartBudget:
    return RestartBudget(cooldown_sec=5.0, window_sec=30.0, max_per_window=3)


class TestRestartBudget:
    def test_first_request_is_granted(self):
        b = make_budget()
        assert b.request(100.0) is RestartVerdict.GRANTED
        assert b.last_restart_ts == 100.0
        assert not b.exhausted

    def test_cooldown_refuses_back_to_back_restarts(self):
        b = make_budget()
        b.request(100.0)
        assert b.request(104.9) is RestartVerdict.COOLING_DOWN
        assert b.request(105.0) is RestartVerdict.GRANTED

    def test_window_limit_exhausts_budget(self):
        b = make_budget()
        assert b.request(0.0) is RestartVerdict.GRANTED
        assert b.request(6.0) is RestartVerdict.GRANTED
        assert b.request(12.0) is RestartVerdict.GRANTED
        assert b.request(18.0) is RestartVerdict.EXHAUSTED
        assert b.exhausted
        # Still refused inside the extended cooldown (2 x window from the last restart).
        assert b.request(12.0 + 59.9) is RestartVerdict.EXHAUSTED
        assert b.last_restart_ts == 12.0

    def test_extended_cooldown_recovers_budget(self):
        b = make_budget()
        for t in (0.0, 6.0, 12.0):
            b.request(t)
        b.request(18.0)
        assert b.exhausted
        assert b.request(12.0 + 60.0) is RestartVerdict.RECOVERED
        assert not b.exhausted
        assert b.last_restart_ts == 72.0
        # Budget is fresh again: two more before it trips.
        assert b.request(78.0) is RestartVerdict.GRANTED
        assert b.request(84.0) is RestartVerdict.GRANTED
        assert b.request(90.0) is RestartVerdict.EXHAUSTED

    def test_events_age_out_of_window_without_exhausting(self):
        b = make_budget()
        # Three restarts spread wider than the window never exhaust.
        for t in (0.0, 20.0, 40.0, 60.0, 80.0):
            assert b.request(t) is RestartVerdict.GRANTED
        assert not b.exhausted

    def test_is_beyond_extended_cooldown(self):
        b = make_budget()
        for t in (0.0, 6.0, 12.0):
            b.request(t)
        assert not b.is_beyond_extended_cooldown(50.0)
        b.request(18.0)  # exhausts
        assert not b.is_beyond_extended_cooldown(71.9)
        assert b.is_beyond_extended_cooldown(72.0)

    def test_reset_clears_everything(self):
        b = make_budget()
        for t in (0.0, 6.0, 12.0, 18.0):
            b.request(t)
        assert b.exhausted
        b.reset()
        assert not b.exhausted
        assert b.last_restart_ts is None
        assert b.request(19.0) is RestartVerdict.GRANTED

    def test_max_per_window_floor_is_one(self):
        b = RestartBudget(cooldown_sec=1.0, window_sec=10.0, max_per_window=0)
        assert b.max_per_window == 1
        assert b.request(0.0) is RestartVerdict.GRANTED
        assert b.request(2.0) is RestartVerdict.EXHAUSTED
