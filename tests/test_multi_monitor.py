import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from browser.emag import AuthenticationRequired
from core.conditions import ProductConfig, ProductObservation
from core.multi_monitor import monitor_products


class FixedClock:
    def __init__(self, current):
        self.current = current
        self.syncs = 0

    def now(self, zone=None):
        return self.current.astimezone(zone) if zone else self.current

    def sync(self):
        self.syncs += 1
        return type(
            "ClockSyncResult",
            (),
            {
                "server": "test-clock",
                "offset_seconds": 0.0,
                "checked_at_utc": datetime.now(timezone.utc),
            },
        )()


def make_config(code: str, target: datetime) -> ProductConfig:
    config = ProductConfig(
        product_url=f"https://www.emag.ro/item/pd/{code}/",
        max_price_ron=Decimal("5.00"),
        quantity=1,
        target_date=target.date().isoformat(),
        target_time=target.strftime("%H:%M:%S"),
        max_attempts=1,
        poll_interval_seconds=10,
        deadline_minutes_after_target=1,
    )
    config.validate()
    return config


class FakeEmag:
    def __init__(self):
        self.opened = []

    def open_product(self, url):
        self.opened.append(url)

    def read_product(self):
        url = self.opened[-1]
        return ProductObservation(
            title=url.rsplit("/", 2)[-2],
            price_ron=Decimal("5.00"),
            in_stock=True,
            can_purchase=True,
            page_url=url,
            observed_at=datetime.now(timezone.utc),
        )


class MultiMonitorTests(unittest.TestCase):
    def test_two_products_have_independent_results_and_event_context(self):
        zone = ZoneInfo("Europe/Bucharest")
        now = datetime.now(zone)
        configs = [
            make_config("AB12CD3BM", now - timedelta(seconds=2)),
            make_config("CD34EF5BM", now - timedelta(seconds=2)),
        ]
        emag = FakeEmag()
        events = []
        statuses = []

        results = monitor_products(
            emag,
            configs,
            clock=FixedClock(now),
            stop_event=threading.Event(),
            resume_event=threading.Event(),
            log=lambda *event: events.append(event),
            on_status=lambda *status: statuses.append(status),
        )

        self.assertEqual([result.index for result in results], [0, 1])
        self.assertEqual([result.state for result in results], ["test_conditions_met"] * 2)
        self.assertEqual(len(emag.opened), 2)
        self.assertEqual(
            [event[2] for event in events if event[2] == "product_check"],
            ["product_check", "product_check"],
        )
        self.assertTrue(all(result.execution_delay_ms >= 0 for result in results))
        self.assertEqual(sum(status[1] == "test_conditions_met" for status in statuses), 2)

    def test_duplicate_or_empty_monitor_list_is_rejected(self):
        zone = ZoneInfo("Europe/Bucharest")
        now = datetime.now(zone)
        config = make_config("AB12CD3BM", now - timedelta(seconds=2))
        events = []
        kwargs = {
            "clock": FixedClock(now),
            "stop_event": threading.Event(),
            "resume_event": threading.Event(),
            "log": lambda *event: events.append(event),
            "on_status": lambda *status: None,
        }
        with self.assertRaises(ValueError):
            monitor_products(FakeEmag(), [], **kwargs)
        with self.assertRaisesRegex(ValueError, "unique eMAG product code"):
            monitor_products(FakeEmag(), [config, config], **kwargs)

    def test_future_product_is_not_opened_before_its_target(self):
        zone = ZoneInfo("Europe/Bucharest")
        now = datetime.now(zone)
        config = make_config("EF56GH7BM", now + timedelta(minutes=5))
        stop = threading.Event()
        timer = threading.Timer(0.05, stop.set)
        timer.start()
        emag = FakeEmag()
        try:
            results = monitor_products(
                emag,
                [config],
                clock=FixedClock(now),
                stop_event=stop,
                resume_event=threading.Event(),
                log=lambda *_event: None,
                on_status=lambda *_status: None,
            )
        finally:
            timer.cancel()
        self.assertEqual(emag.opened, [])
        self.assertEqual(results[0].state, "stopped")

    def test_manual_login_pauses_other_product_navigation_until_resumed(self):
        zone = ZoneInfo("Europe/Bucharest")
        now = datetime.now(zone)
        configs = [
            make_config("AB12CD3BM", now - timedelta(seconds=2)),
            make_config("CD34EF5BM", now - timedelta(seconds=2)),
        ]
        resume = threading.Event()

        class LoginOnceEmag(FakeEmag):
            def open_product(self, url):
                if not self.opened:
                    self.opened.append(url)
                    raise AuthenticationRequired("Sign in manually.")
                super().open_product(url)

        emag = LoginOnceEmag()
        timer = threading.Timer(0.05, resume.set)
        timer.start()
        try:
            results = monitor_products(
                emag,
                configs,
                clock=FixedClock(now),
                stop_event=threading.Event(),
                resume_event=resume,
                log=lambda *_event: None,
                on_status=lambda *_status: None,
            )
        finally:
            timer.cancel()
        self.assertEqual([result.state for result in results], ["test_conditions_met"] * 2)
        self.assertEqual(emag.opened.count(configs[1].product_url), 1)


if __name__ == "__main__":
    unittest.main()
