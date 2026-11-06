import unittest
from decimal import Decimal

from core.conditions import ConfigurationError, ProductConfig, validate_emag_product_url
from core.markets import market_for_url, parse_market_price


class MarketTests(unittest.TestCase):
    def test_hungary_market_currency_timezone_and_canonical_url(self):
        url = "https://www.emag.hu/termek/pd/AB12CD3BM/"
        market = market_for_url(url)
        self.assertEqual(market.code, "hu")
        self.assertEqual(market.currency, "HUF")
        self.assertEqual(market.timezone_name, "Europe/Budapest")
        self.assertFalse(market.verified_for_automation)
        self.assertEqual(validate_emag_product_url(url), url)

    def test_rejects_short_link_that_live_site_redirected_from(self):
        self.assertEqual(
            market_for_url("https://www.emag.hu/R944240_1").code,
            "hu",
        )
        with self.assertRaisesRegex(ConfigurationError, "canonical URL"):
            validate_emag_product_url("https://www.emag.hu/R944240_1")

    def test_hungary_short_link_does_not_pass_product_configuration(self):
        config = ProductConfig(
            product_url="https://www.emag.hu/R944240_1",
            max_price_ron=Decimal("50000"),
            quantity=1,
            target_date="2026-11-06",
            target_time="12:00:00",
            max_attempts=1,
            timezone_name="Europe/Budapest",
        )
        with self.assertRaises(ConfigurationError):
            config.validate()

    def test_parses_one_native_market_amount(self):
        self.assertEqual(parse_market_price("1.178,67 €", "EUR"), Decimal("1178.67"))
        self.assertEqual(parse_market_price("54 990 Ft", "HUF"), Decimal("54990"))
        self.assertEqual(parse_market_price("1.234,50 lei", "RON"), Decimal("1234.50"))

    def test_price_parser_rejects_old_price_plus_current_price(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            parse_market_price("59,90 € 39,99 €", "EUR")

    def test_price_parser_rejects_wrong_currency_or_unknown_currency(self):
        with self.assertRaises(ValueError):
            parse_market_price("12 990 Ft", "EUR")
        with self.assertRaises(ValueError):
            parse_market_price("12 lei", "BGN")

    def test_hungarian_schedule_requires_budapest_timezone(self):
        config = ProductConfig(
            product_url="https://www.emag.hu/termek/pd/AB12CD3BM/",
            max_price_ron=Decimal("50000"),
            quantity=1,
            target_date="2026-11-06",
            target_time="12:00:00",
            max_attempts=1,
            timezone_name="Europe/Budapest",
        )
        self.assertEqual(config.target_datetime().tzinfo.key, "Europe/Budapest")

    def test_unknown_market_timezone_is_rejected(self):
        config = ProductConfig(
            product_url="https://www.emag.hu/termek/pd/AB12CD3BM/",
            max_price_ron=Decimal("50000"),
            quantity=1,
            target_date="2026-11-06",
            target_time="12:00:00",
            max_attempts=1,
            timezone_name="Europe/Bucharest",
        )
        with self.assertRaisesRegex(ConfigurationError, "Europe/Budapest"):
            config.target_datetime()


if __name__ == "__main__":
    unittest.main()
