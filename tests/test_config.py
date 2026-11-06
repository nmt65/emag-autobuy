import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core.config import AppSettings, load_settings, save_settings
from core.conditions import ConfigurationError, ProductConfig


class ConfigTests(unittest.TestCase):
    def test_yaml_settings_round_trip(self):
        product = ProductConfig(
            product_url="https://www.emag.ro/item/pd/AB12CD3BM/",
            max_price_ron=Decimal("199.95"),
            quantity=2,
            target_date="2026-11-06",
            target_time="12:30:45",
            max_attempts=4,
            is_digital_ticket=True,
        )
        second_product = ProductConfig(
            product_url="https://www.emag.ro/another-item/pd/CD34EF5BM/",
            max_price_ron=Decimal("5.00"),
            quantity=1,
            target_date="2026-11-07",
            target_time="08:00:00",
            max_attempts=2,
        )
        settings = AppSettings(
            product=product,
            additional_products=(second_product,),
            poll_interval_seconds=30,
            deadline_minutes_after_target=20,
            ntp_servers=("time.cloudflare.com",),
            browser_mode="chrome_cdp",
            browser_cdp_endpoint="http://127.0.0.1:9222",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            save_settings(settings, path)
            loaded = load_settings(path)

        self.assertEqual(loaded.product.product_url, product.product_url)
        self.assertEqual(loaded.product.max_price_ron, Decimal("199.95"))
        self.assertEqual(loaded.product.quantity, 2)
        self.assertEqual(len(loaded.products), 2)
        self.assertEqual(loaded.products[1].product_url, second_product.product_url)
        self.assertEqual(loaded.products[1].max_price_ron, Decimal("5.00"))
        self.assertTrue(loaded.product.is_digital_ticket)
        self.assertEqual(loaded.ntp_servers, ("time.cloudflare.com",))
        self.assertEqual(loaded.browser_mode, "chrome_cdp")
        self.assertEqual(loaded.browser_cdp_endpoint, "http://127.0.0.1:9222")

    def test_hungary_product_round_trips_native_currency_and_timezone(self):
        product = ProductConfig(
            product_url="https://www.emag.hu/termek/pd/AB12CD3BM/",
            max_price_ron=Decimal("59990"),
            quantity=2,
            target_date="2026-11-06",
            target_time="12:30:45",
            max_attempts=2,
            timezone_name="Europe/Budapest",
        )
        settings = AppSettings(product=product, timezone_name="Europe/Budapest")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hungary.yaml"
            save_settings(settings, path)
            loaded = load_settings(path)

        self.assertEqual(loaded.product.max_price_ron, Decimal("59990"))
        self.assertEqual(loaded.product.currency, "HUF")
        self.assertEqual(loaded.product.timezone_name, "Europe/Budapest")
        self.assertEqual(loaded.product.target_datetime().tzinfo.key, "Europe/Budapest")

    def test_legacy_single_product_yaml_still_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.yaml"
            path.write_text(
                "timezone: Europe/Bucharest\n"
                "product:\n"
                "  product_url: https://www.emag.ro/item/pd/AB12CD3BM/\n"
                "  max_price_ron: 5.00\n"
                "  quantity: 1\n"
                "  target_date: '2026-11-06'\n"
                "  target_time: '12:00:00'\n"
                "  max_attempts: 1\n",
                encoding="utf-8",
            )
            settings = load_settings(path)
        self.assertEqual(len(settings.products), 1)
        self.assertEqual(settings.product.max_price_ron, Decimal("5.00"))
        self.assertFalse(settings.product.is_digital_ticket)

    def test_bucharest_target_is_timezone_aware_and_requires_seconds(self):
        product = ProductConfig(
            product_url="https://www.emag.ro/item/pd/AB12CD3BM/",
            max_price_ron=Decimal("100"),
            quantity=1,
            target_date="2026-11-06",
            target_time="12:30:45",
            max_attempts=1,
        )
        self.assertEqual(product.target_datetime().tzinfo.key, "Europe/Bucharest")
        with self.assertRaises(ConfigurationError):
            ProductConfig(
                product_url=product.product_url,
                max_price_ron=product.max_price_ron,
                quantity=product.quantity,
                target_date=product.target_date,
                target_time="12:30",
                max_attempts=1,
            ).target_datetime()
        with self.assertRaises(ConfigurationError):
            ProductConfig(
                product_url=product.product_url,
                max_price_ron=product.max_price_ron,
                quantity=product.quantity,
                target_date=product.target_date,
                target_time=product.target_time,
                max_attempts=product.max_attempts,
                timezone_name="UTC",
            ).target_datetime()

    def test_bucharest_dst_gap_and_repeated_hour_are_rejected(self):
        base = {
            "product_url": "https://www.emag.ro/item/pd/AB12CD3BM/",
            "max_price_ron": Decimal("100"),
            "quantity": 1,
            "max_attempts": 1,
        }
        for target_date, target_time in (
            ("2026-03-29", "03:30:00"),
            ("2026-10-25", "03:30:00"),
        ):
            with self.subTest(target_date=target_date), self.assertRaises(ConfigurationError):
                ProductConfig(
                    **base,
                    target_date=target_date,
                    target_time=target_time,
                ).target_datetime()


if __name__ == "__main__":
    unittest.main()
