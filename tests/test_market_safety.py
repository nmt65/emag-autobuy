import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core.conditions import ProductConfig
from core.runtime import WorkerController


class MarketSafetyTests(unittest.TestCase):
    def test_hungary_automation_stops_before_browser_or_database_work(self):
        config = ProductConfig(
            product_url="https://www.emag.hu/termek/pd/AB12CD3BM/",
            max_price_ron=Decimal("50000"),
            quantity=1,
            target_date="2026-11-06",
            target_time="12:00:00",
            max_attempts=1,
            timezone_name="Europe/Budapest",
        )
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "must-not-be-created.sqlite3"
            with self.assertRaisesRegex(ValueError, "real product and checkout DOM"):
                WorkerController().start(
                    config,
                    test_mode=True,
                    profile_dir=Path(directory) / "profile",
                    database_path=database_path,
                    ntp_servers=("time.cloudflare.com",),
                )
            self.assertFalse(database_path.exists())


if __name__ == "__main__":
    unittest.main()
