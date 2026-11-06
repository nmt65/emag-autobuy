import threading
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from browser.emag import PlaywrightError
from core.conditions import ProductConfig, ProductObservation
from core.monitor import monitor_until_ready


class FakeClock:
    def __init__(self, current):
        self.current = current

    def now(self, zone=None):
        return self.current.astimezone(zone) if zone else self.current


class AdvancingStopEvent:
    def __init__(self, clock):
        self.clock = clock
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.clock.current += timedelta(seconds=seconds)
        return self.stopped


class FakeEmag:
    def __init__(self):
        self.opens = 0
        self.reads = 0

    def open_product(self, url):
        self.opens += 1
        if self.opens == 1:
            raise PlaywrightError("temporary network error")

    def read_product(self):
        self.reads += 1
        return ProductObservation(
            title="Sample product",
            price_ron=Decimal("100.00"),
            in_stock=True,
            can_purchase=True,
            page_url="https://www.emag.ro/item/pd/AB12CD3BM/",
            observed_at=datetime.now().astimezone(),
        )


class MonitorErrorHandlingTests(unittest.TestCase):
    def test_network_error_is_logged_and_retried(self):
        zone = ZoneInfo("Europe/Bucharest")
        now = datetime(2026, 11, 5, 12, 0, 0, tzinfo=zone)
        clock = FakeClock(now)
        stop = AdvancingStopEvent(clock)
        emag = FakeEmag()
        events = []
        config = ProductConfig(
            product_url="https://www.emag.ro/item/pd/AB12CD3BM/",
            max_price_ron=Decimal("200.00"),
            quantity=1,
            target_date="2026-11-05",
            target_time="12:00:00",
            max_attempts=2,
            poll_interval_seconds=10,
        )
        found = monitor_until_ready(
            emag,
            config,
            deadline=now + timedelta(minutes=1),
            clock=clock,
            stop_event=stop,
            log=lambda *event: events.append(event),
        )
        self.assertIsNotNone(found)
        self.assertEqual(emag.opens, 2)
        self.assertTrue(any(event[1] == "browser_retry" for event in events))


if __name__ == "__main__":
    unittest.main()
