import tempfile
import time
import unittest
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from browser.emag import CartSnapshot, CheckoutReview, EmagOrderResult
from core.conditions import ProductConfig, ProductObservation
from core.runtime import WorkerController
from core.scheduler import ClockSync
from database.db import Database


class FakeClock:
    def __init__(self):
        self.sync_count = 0

    def sync(self):
        self.sync_count += 1
        return ClockSync(
            server="test-clock",
            offset_seconds=0.0,
            checked_at_utc=datetime.now(timezone.utc),
        )

    def now(self, zone=None):
        current = datetime.now(timezone.utc)
        return current.astimezone(zone) if zone else current


class FakePage:
    def goto(self, *_args, **_kwargs):
        pass


class FakeEmagPage:
    def __init__(self, _page):
        self.current_url = ""

    def open_product(self, url):
        self.current_url = url

    def read_product(self):
        return ProductObservation(
            title=self.current_url.rsplit("/", 2)[-2],
            price_ron=Decimal("5.00"),
            in_stock=True,
            can_purchase=True,
            page_url=self.current_url,
            observed_at=datetime.now(timezone.utc),
        )


class FakeBrowserManager:
    def __init__(self, _profile, **_kwargs):
        pass

    def __enter__(self):
        return FakePage()

    def __exit__(self, *_args):
        pass


class FakeLiveEmag:
    def __init__(self, _page, *, total=Decimal("100.00")):
        self.total = total
        self.prepare_calls = 0
        self.submit_calls = 0

    def prepare_cart(self, _config, product):
        self.prepare_calls += 1
        return CartSnapshot(
            visible_text=product.title,
            product_present=True,
            cart_unit_price_ron=Decimal("99.00"),
            quantity_field=None,
            quantity=1,
            cart_total_ron=self.total,
            product_subtotal_ron=Decimal("99.00"),
            cart_known_empty=False,
            ambiguous_product_reference=False,
            quantity_control_count=1,
        )

    def proceed_to_checkout(self, config, product):
        return CheckoutReview(
            product_title=product.title,
            quantity=config.quantity,
            cart_total_ron=self.total,
            payment_options=("Card online",),
            checkout_url="https://www.emag.ro/cart/checkout",
            product_subtotal_ron=Decimal("99.00"),
            digital_delivery_confirmed=True,
        )

    def validate_order_submission(
        self,
        _total,
        *,
        expected_product_subtotal_ron=None,
        expected_product_titles=(),
    ):
        pass

    def submit_order_once(
        self,
        _total,
        *,
        stop_event=None,
        expected_product_subtotal_ron=None,
        expected_product_titles=(),
    ):
        if stop_event is not None and stop_event.is_set():
            return EmagOrderResult("cancelled_before_click", "Stopped before submit.")
        self.submit_calls += 1
        return EmagOrderResult(
            "order_placed_verified", "Fixture confirmation shown."
        )


class FakeSharedCartEmag:
    def __init__(self, _page, *, total=Decimal("100.00")):
        self.total = total
        self.opened_url = ""
        self.prepare_calls = 0
        self.submit_calls = 0
        self.expected_titles = ()

    def open_product(self, url):
        self.opened_url = url

    def read_product(self):
        code = self.opened_url.split("/pd/")[-1].strip("/").upper()
        title = {"AA11BB2BM": "Digital ticket alpha", "CC33DD4BM": "Digital ticket beta"}[code]
        price = Decimal("40.00") if code == "AA11BB2BM" else Decimal("45.00")
        return ProductObservation(
            title=title,
            price_ron=price,
            in_stock=True,
            can_purchase=True,
            page_url=self.opened_url,
            observed_at=datetime.now(timezone.utc),
        )

    def prepare_multi_cart(self, configs, products):
        self.prepare_calls += 1
        subtotal = sum(
            (product.price_ron * config.quantity for config, product in zip(configs, products, strict=True)),
            Decimal("0.00"),
        )
        return tuple(
            CartSnapshot(
                visible_text=" ".join(product.title for product in products),
                product_present=True,
                cart_unit_price_ron=product.price_ron,
                quantity_field=None,
                quantity=config.quantity,
                cart_total_ron=self.total,
                product_subtotal_ron=subtotal,
                cart_known_empty=False,
                ambiguous_product_reference=False,
                quantity_control_count=len(configs),
            )
            for config, product in zip(configs, products, strict=True)
        )

    def proceed_to_checkout_multi(self, configs, products):
        subtotal = sum(
            (product.price_ron * config.quantity for config, product in zip(configs, products, strict=True)),
            Decimal("0.00"),
        )
        return CheckoutReview(
            product_title=f"Shared cart ({len(products)} products)",
            quantity=sum(config.quantity for config in configs),
            cart_total_ron=self.total,
            payment_options=("Card online",),
            checkout_url="https://www.emag.ro/cart/checkout",
            product_subtotal_ron=subtotal,
            digital_delivery_confirmed=True,
        )

    def validate_order_submission(self, _total, **_kwargs):
        pass

    def submit_order_once(self, _total, *, stop_event=None, **kwargs):
        self.expected_titles = kwargs.get("expected_product_titles", ())
        if stop_event is not None and stop_event.is_set():
            return EmagOrderResult("cancelled_before_click", "Stopped before submit.")
        self.submit_calls += 1
        return EmagOrderResult("order_placed_verified", "Shared order confirmed in fixture.")


class RuntimeTests(unittest.TestCase):
    def test_worker_schedules_asynchronously_and_test_mode_finishes_after_match(self):
        zone = ZoneInfo("Europe/Bucharest")
        target = datetime.now(zone).replace(microsecond=0) + timedelta(seconds=2)
        config = ProductConfig(
            product_url="https://www.emag.ro/item/pd/AB12CD3BM/",
            max_price_ron=Decimal("100.00"),
            quantity=1,
            target_date=target.date().isoformat(),
            target_time=target.strftime("%H:%M:%S"),
            max_attempts=1,
            poll_interval_seconds=10,
            is_digital_ticket=True,
        )
        observation = ProductObservation(
            title="Sample product",
            price_ron=Decimal("100.00"),
            in_stock=True,
            can_purchase=True,
            page_url=config.product_url,
            observed_at=target,
        )
        fake_clock = FakeClock()
        monitor_called_at = []

        def monitor_stub(*_args, **_kwargs):
            monitor_called_at.append(fake_clock.now(zone))
            return observation

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "events.sqlite3"
            controller = WorkerController()
            with (
                patch("core.runtime.SynchronizedClock", return_value=fake_clock),
                patch("core.runtime.BrowserManager", FakeBrowserManager),
                patch("core.runtime.monitor_until_ready", side_effect=monitor_stub),
                patch("core.runtime.CheckoutPreparer") as checkout_preparer,
            ):
                run_id = controller.start(
                    config,
                    test_mode=True,
                    profile_dir=root / "profile",
                    database_path=database_path,
                    ntp_servers=("test-clock",),
                )
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    status = controller.status()
                    if status["state"] in {"test_conditions_met", "error"}:
                        break
                    time.sleep(0.05)
                while controller.is_running() and time.monotonic() < deadline:
                    time.sleep(0.05)

            self.assertFalse(controller.is_running())
            self.assertEqual(controller.status()["state"], "test_conditions_met")
            self.assertEqual(fake_clock.sync_count, 2)
            self.assertEqual(len(monitor_called_at), 1)
            self.assertGreaterEqual(monitor_called_at[0], target)
            checkout_preparer.assert_not_called()
            run = Database(database_path).latest_run()
            self.assertEqual(run["id"], run_id)
            self.assertEqual(run["status"], "test_conditions_met")

    def test_multi_product_test_mode_tracks_each_product_without_cart_actions(self):
        zone = ZoneInfo("Europe/Bucharest")
        target = datetime.now(zone).replace(microsecond=0)
        configs = [
            ProductConfig(
                product_url=f"https://www.emag.ro/item/pd/{code}/",
                max_price_ron=Decimal("5.00"),
                quantity=1,
                target_date=target.date().isoformat(),
                target_time=target.strftime("%H:%M:%S"),
                max_attempts=1,
                poll_interval_seconds=10,
                deadline_minutes_after_target=1,
            )
            for code in ("AB12CD3BM", "CD34EF5BM")
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "multi.sqlite3"
            controller = WorkerController()
            clock = FakeClock()
            with (
                patch("core.runtime.SynchronizedClock", return_value=clock),
                patch("core.runtime.BrowserManager", FakeBrowserManager),
                patch("core.runtime.EmagPage", FakeEmagPage),
                patch("core.runtime.CheckoutPreparer") as checkout_preparer,
            ):
                run_id = controller.start(
                    configs,
                    test_mode=True,
                    profile_dir=root / "profile",
                    database_path=database_path,
                    ntp_servers=("test-clock",),
                )
                deadline = time.monotonic() + 8
                while controller.is_running() and time.monotonic() < deadline:
                    time.sleep(0.05)

            self.assertFalse(controller.is_running())
            self.assertEqual(controller.status()["state"], "test_completed")
            product_states = controller.status()["products"]
            self.assertEqual(len(product_states), 2)
            self.assertEqual(
                [item["state"] for item in product_states],
                ["test_conditions_met", "test_conditions_met"],
            )
            checkout_preparer.assert_not_called()
            run = Database(database_path).latest_run()
            self.assertEqual(run["id"], run_id)
            self.assertEqual(run["status"], "test_completed")
            events = Database(database_path).recent_events(100)
            product_checks = [event for event in events if event["event_type"] == "product_check"]
            self.assertEqual(len(product_checks), 2)

    def test_multi_product_live_workflow_accepts_and_can_be_stopped_before_target(self):
        zone = ZoneInfo("Europe/Bucharest")
        target = datetime.now(zone).replace(microsecond=0) + timedelta(minutes=5)
        products = [
            ProductConfig(
                product_url=f"https://www.emag.ro/item/pd/{code}/",
                max_price_ron=Decimal("5.00"),
                quantity=1,
                target_date=target.date().isoformat(),
                target_time=target.strftime("%H:%M:%S"),
                max_attempts=1,
            )
            for code in ("AB12CD3BM", "CD34EF5BM")
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = WorkerController()
            with (
                patch("core.runtime.SynchronizedClock", return_value=FakeClock()),
                patch("core.runtime.BrowserManager", FakeBrowserManager),
                patch("core.runtime.EmagPage", FakeEmagPage),
            ):
                run_id = controller.start(
                    products,
                    test_mode=False,
                    profile_dir=root / "profile",
                    database_path=root / "db.sqlite3",
                    ntp_servers=("test-clock",),
                )
                controller.stop()
                deadline = time.monotonic() + 8
                while controller.is_running() and time.monotonic() < deadline:
                    time.sleep(0.05)
            self.assertFalse(controller.is_running())
            self.assertEqual(controller.status()["state"], "stopped")
            self.assertEqual(Database(root / "db.sqlite3").latest_run()["id"], run_id)

    def test_multi_product_live_flow_uses_one_shared_order_under_100_ron(self):
        zone = ZoneInfo("Europe/Bucharest")
        target = datetime.now(zone).replace(microsecond=0)
        configs = [
            ProductConfig(
                product_url=f"https://www.emag.ro/ticket/pd/{code}/",
                max_price_ron=Decimal("50.00"),
                quantity=1,
                target_date=target.date().isoformat(),
                target_time=target.strftime("%H:%M:%S"),
                max_attempts=1,
                poll_interval_seconds=10,
                is_digital_ticket=True,
            )
            for code in ("AA11BB2BM", "CC33DD4BM")
        ]
        instances = []

        def emag_factory(page):
            instance = FakeSharedCartEmag(page)
            instances.append(instance)
            return instance

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "multi-order.sqlite3"
            controller = WorkerController()
            with (
                patch("core.runtime.SynchronizedClock", return_value=FakeClock()),
                patch("core.runtime.BrowserManager", FakeBrowserManager),
                patch("core.runtime.EmagPage", side_effect=emag_factory),
            ):
                controller.start(
                    configs,
                    test_mode=False,
                    profile_dir=root / "profile",
                    database_path=database_path,
                    ntp_servers=("test-clock",),
                )
                deadline = time.monotonic() + 12
                while controller.is_running() and time.monotonic() < deadline:
                    time.sleep(0.05)

            self.assertFalse(controller.is_running())
            self.assertEqual(controller.status()["state"], "order_placed_verified")
            self.assertEqual(instances[-1].prepare_calls, 1)
            self.assertEqual(instances[-1].submit_calls, 1)
            self.assertEqual(
                instances[-1].expected_titles,
                ("Digital ticket alpha", "Digital ticket beta"),
            )
            self.assertEqual(Database(database_path).latest_run()["status"], "order_placed_verified")

            second_controller = WorkerController()
            with (
                patch("core.runtime.SynchronizedClock", return_value=FakeClock()),
                patch("core.runtime.BrowserManager", FakeBrowserManager),
                patch("core.runtime.EmagPage", side_effect=emag_factory),
            ):
                second_controller.start(
                    configs,
                    test_mode=False,
                    profile_dir=root / "profile",
                    database_path=database_path,
                    ntp_servers=("test-clock",),
                )
                deadline = time.monotonic() + 12
                while second_controller.is_running() and time.monotonic() < deadline:
                    time.sleep(0.05)
            self.assertEqual(second_controller.status()["state"], "duplicate_order_blocked")
            self.assertEqual(instances[-1].submit_calls, 0)

    def test_multi_product_live_flow_holds_for_manual_review_over_100_ron(self):
        zone = ZoneInfo("Europe/Bucharest")
        target = datetime.now(zone).replace(microsecond=0)
        configs = [
            ProductConfig(
                product_url=f"https://www.emag.ro/ticket/pd/{code}/",
                max_price_ron=Decimal("50.00"),
                quantity=1,
                target_date=target.date().isoformat(),
                target_time=target.strftime("%H:%M:%S"),
                max_attempts=1,
                poll_interval_seconds=10,
                is_digital_ticket=True,
            )
            for code in ("AA11BB2BM", "CC33DD4BM")
        ]
        instances = []

        def emag_factory(page):
            instance = FakeSharedCartEmag(page, total=Decimal("100.01"))
            instances.append(instance)
            return instance

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = WorkerController()
            with (
                patch("core.runtime.SynchronizedClock", return_value=FakeClock()),
                patch("core.runtime.BrowserManager", FakeBrowserManager),
                patch("core.runtime.EmagPage", side_effect=emag_factory),
            ):
                controller.start(
                    configs,
                    test_mode=False,
                    profile_dir=root / "profile",
                    database_path=root / "multi-over-cap.sqlite3",
                    ntp_servers=("test-clock",),
                )
                deadline = time.monotonic() + 12
                while (
                    controller.status()["state"] != "awaiting_manual_order_review"
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                self.assertEqual(
                    controller.status()["state"], "awaiting_manual_order_review"
                )
                self.assertTrue(controller.is_running())
                controller.stop()
                deadline = time.monotonic() + 8
                while controller.is_running() and time.monotonic() < deadline:
                    time.sleep(0.05)

            self.assertEqual(
                controller.status()["state"], "stopped_after_manual_order_review"
            )
            self.assertEqual(instances[-1].submit_calls, 0)

    def test_single_product_live_flow_submits_only_once_under_the_hard_cap(self):
        zone = ZoneInfo("Europe/Bucharest")
        target = datetime.now(zone).replace(microsecond=0)
        config = ProductConfig(
            product_url="https://www.emag.ro/ticket/pd/AB12CD3BM/",
            max_price_ron=Decimal("100.00"),
            quantity=1,
            target_date=target.date().isoformat(),
            target_time=target.strftime("%H:%M:%S"),
            max_attempts=1,
            poll_interval_seconds=10,
            is_digital_ticket=True,
        )
        observation = ProductObservation(
            title="Sample digital ticket",
            price_ron=Decimal("99.00"),
            in_stock=True,
            can_purchase=True,
            page_url=config.product_url,
            observed_at=target,
        )
        instances = []

        def emag_factory(page):
            created = FakeLiveEmag(page)
            instances.append(created)
            return created

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "live-order.sqlite3"
            patches = (
                patch("core.runtime.SynchronizedClock", return_value=FakeClock()),
                patch("core.runtime.BrowserManager", FakeBrowserManager),
                patch("core.runtime.EmagPage", side_effect=emag_factory),
                patch("core.runtime.monitor_until_ready", return_value=observation),
            )
            with patches[0], patches[1], patches[2], patches[3]:
                controller = WorkerController()
                first_run = controller.start(
                    config,
                    test_mode=False,
                    profile_dir=root / "profile",
                    database_path=database_path,
                    ntp_servers=("test-clock",),
                )
                deadline = time.monotonic() + 8
                while controller.is_running() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(controller.status()["state"], "order_placed_verified")
                self.assertEqual(instances[-1].submit_calls, 1)

                second_controller = WorkerController()
                second_run = second_controller.start(
                    config,
                    test_mode=False,
                    profile_dir=root / "profile",
                    database_path=database_path,
                    ntp_servers=("test-clock",),
                )
                deadline = time.monotonic() + 8
                while second_controller.is_running() and time.monotonic() < deadline:
                    time.sleep(0.05)

            self.assertEqual(second_controller.status()["state"], "duplicate_order_blocked")
            self.assertEqual(instances[-1].submit_calls, 0)
            db = Database(database_path)
            self.assertEqual(db.latest_run()["id"], second_run)
            key = hashlib.sha256(
                f"ab12cd3bm|{config.target_datetime().isoformat()}|1".encode("utf-8")
            ).hexdigest()
            self.assertEqual(db.order_submission(key)["status"], "order_placed_verified")
            self.assertNotEqual(first_run, second_run)

    def test_single_product_live_flow_blocks_checkout_over_100_without_submit(self):
        zone = ZoneInfo("Europe/Bucharest")
        target = datetime.now(zone).replace(microsecond=0)
        config = ProductConfig(
            product_url="https://www.emag.ro/ticket/pd/CD34EF5BM/",
            max_price_ron=Decimal("150.00"),
            quantity=1,
            target_date=target.date().isoformat(),
            target_time=target.strftime("%H:%M:%S"),
            max_attempts=1,
            poll_interval_seconds=10,
            is_digital_ticket=True,
        )
        observation = ProductObservation(
            title="Sample digital ticket",
            price_ron=Decimal("99.00"),
            in_stock=True,
            can_purchase=True,
            page_url=config.product_url,
            observed_at=target,
        )
        instances = []

        def emag_factory(page):
            created = FakeLiveEmag(page, total=Decimal("100.01"))
            instances.append(created)
            return created

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "manual-review.sqlite3"
            controller = WorkerController()
            with (
                patch("core.runtime.SynchronizedClock", return_value=FakeClock()),
                patch("core.runtime.BrowserManager", FakeBrowserManager),
                patch("core.runtime.EmagPage", side_effect=emag_factory),
                patch("core.runtime.monitor_until_ready", return_value=observation),
            ):
                controller.start(
                    config,
                    test_mode=False,
                    profile_dir=root / "profile",
                    database_path=database_path,
                    ntp_servers=("test-clock",),
                )
                deadline = time.monotonic() + 8
                while (
                    controller.status()["state"] != "awaiting_manual_order_review"
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                self.assertEqual(
                    controller.status()["state"], "awaiting_manual_order_review"
                )
                self.assertTrue(controller.is_running())
                controller.stop()
                deadline = time.monotonic() + 8
                while controller.is_running() and time.monotonic() < deadline:
                    time.sleep(0.05)

                second_controller = WorkerController()
                second_controller.start(
                    config,
                    test_mode=False,
                    profile_dir=root / "profile",
                    database_path=database_path,
                    ntp_servers=("test-clock",),
                )
                deadline = time.monotonic() + 8
                while second_controller.is_running() and time.monotonic() < deadline:
                    time.sleep(0.05)
            self.assertEqual(
                controller.status()["state"], "stopped_after_manual_order_review"
            )
            self.assertEqual(instances[-1].submit_calls, 0)
            self.assertEqual(second_controller.status()["state"], "duplicate_order_blocked")
            self.assertEqual(instances[-1].prepare_calls, 0)


if __name__ == "__main__":
    unittest.main()
