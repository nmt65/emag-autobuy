import tempfile
import unittest
from pathlib import Path

from database.db import Database


class DatabaseTests(unittest.TestCase):
    def test_run_and_event_history_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "history.sqlite3")
            run_id = database.start_run(
                {"product_url": "https://www.emag.ro/item/pd/AB12CD3BM/"},
                test_mode=True,
            )
            database.add_event(
                run_id,
                "INFO",
                "test_event",
                "History entry",
                {"count": 1},
            )
            database.set_run_status(run_id, "complete", "Finished", finished=True)

            latest = database.latest_run()
            events = database.recent_events()

        self.assertEqual(latest["id"], run_id)
        self.assertEqual(latest["status"], "complete")
        self.assertTrue(events[0]["timestamp_local"])
        self.assertEqual(events[0]["event_type"], "test_event")
        self.assertEqual(events[0]["run_id"], run_id)

    def test_order_submission_claim_is_durable_and_blocks_duplicate_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "orders.sqlite3")
            first_run = database.start_run({"product": "A"}, test_mode=False)
            second_run = database.start_run({"product": "A"}, test_mode=False)
            key = "stable-idempotency-key"

            self.assertTrue(database.claim_order_submission(key, first_run))
            database.set_order_submission_status(
                key, "order_status_unknown", "Outcome not confirmed."
            )
            self.assertFalse(database.claim_order_submission(key, second_run))
            record = database.order_submission(key)

        self.assertEqual(record["status"], "order_status_unknown")
        self.assertEqual(record["run_id"], first_run)


if __name__ == "__main__":
    unittest.main()
