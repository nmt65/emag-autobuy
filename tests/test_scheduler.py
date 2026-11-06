import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from core.scheduler import ClockSync, SynchronizedClock, execution_delay_ms, wait_until


class FakeClock:
    def __init__(self, current):
        self.current = current
        self.observed = []

    def now(self, zone=None):
        value = self.current.astimezone(zone) if zone else self.current
        self.observed.append(value)
        return value


class AdvancingStopEvent:
    def __init__(self, clock):
        self.clock = clock
        self.stopped = False

    def wait(self, seconds):
        self.clock.current += timedelta(seconds=seconds)
        return self.stopped

    def is_set(self):
        return self.stopped


class SchedulerTests(unittest.TestCase):
    def test_scheduler_never_returns_before_target(self):
        zone = ZoneInfo("Europe/Bucharest")
        start = datetime(2026, 11, 5, 12, 0, 0, tzinfo=zone)
        target = start + timedelta(seconds=1)
        clock = FakeClock(start)
        result = wait_until(
            target,
            clock,
            AdvancingStopEvent(clock),
            precision_seconds=0.2,
        )
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result.actual, target)
        self.assertTrue(all(value <= result.actual for value in clock.observed))
        self.assertGreaterEqual(result.delay_ms, 0)

    def test_scheduler_can_be_cancelled(self):
        zone = ZoneInfo("Europe/Bucharest")
        start = datetime(2026, 11, 5, 12, 0, 0, tzinfo=zone)
        target = start + timedelta(hours=1)
        clock = FakeClock(start)
        stopped = threading.Event()
        stopped.set()
        self.assertIsNone(wait_until(target, clock, stopped))

    def test_execution_delay_is_nonnegative(self):
        zone = ZoneInfo("Europe/Bucharest")
        target = datetime(2026, 11, 5, 12, 0, 0, tzinfo=zone)
        self.assertEqual(execution_delay_ms(target, target - timedelta(seconds=1)), 0)
        self.assertEqual(execution_delay_ms(target, target + timedelta(milliseconds=125)), 125)

    def test_synchronized_clock_uses_monotonic_elapsed_time(self):
        anchor = datetime(2026, 11, 5, 10, 0, 0, tzinfo=ZoneInfo("UTC"))
        clock = SynchronizedClock(("unused",))
        clock.sync_result = ClockSync("test", 0.25, anchor)
        clock._anchor_utc = anchor
        clock._anchor_monotonic = 100.0
        with patch("core.scheduler.time.monotonic", return_value=101.0):
            self.assertEqual(
                clock.now(ZoneInfo("UTC")),
                anchor + timedelta(seconds=1.25),
            )


if __name__ == "__main__":
    unittest.main()
