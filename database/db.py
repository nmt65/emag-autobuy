from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo


LOCAL_ZONE = ZoneInfo("Europe/Bucharest")


class Database:
    """Small SQLite event store; every operation owns its connection."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _timestamps() -> tuple[str, str]:
        utc_now = datetime.now(timezone.utc)
        return (
            utc_now.isoformat(timespec="milliseconds"),
            utc_now.astimezone(LOCAL_ZONE).isoformat(timespec="milliseconds"),
        )

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    test_mode INTEGER NOT NULL,
                    config_json TEXT NOT NULL,
                    started_at_utc TEXT NOT NULL,
                    started_at_local TEXT NOT NULL,
                    finished_at_utc TEXT,
                    finished_at_local TEXT,
                    status_message TEXT NOT NULL DEFAULT '',
                    current_price_ron TEXT,
                    availability TEXT,
                    execution_delay_ms REAL,
                    review_json TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT REFERENCES runs(id),
                    timestamp_utc TEXT NOT NULL,
                    timestamp_local TEXT NOT NULL,
                    level TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_events_timestamp
                    ON events(timestamp_utc DESC);
                CREATE TABLE IF NOT EXISTS order_intents (
                    idempotency_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    status TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT ''
                );
                """
            )

    def start_run(self, config: dict[str, Any], test_mode: bool) -> str:
        run_id = str(uuid.uuid4())
        utc_stamp, local_stamp = self._timestamps()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO runs
                   (id, status, test_mode, config_json, started_at_utc, started_at_local)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    "starting",
                    int(test_mode),
                    json.dumps(config, ensure_ascii=False, default=str),
                    utc_stamp,
                    local_stamp,
                ),
            )
        return run_id

    def set_run_status(
        self,
        run_id: str,
        status: str,
        message: str,
        *,
        finished: bool = False,
        current_price_ron: str | None = None,
        availability: str | None = None,
        execution_delay_ms: float | None = None,
        review: dict[str, Any] | None = None,
    ) -> None:
        utc_stamp, local_stamp = self._timestamps()
        with self._connect() as connection:
            connection.execute(
                """UPDATE runs SET
                    status = ?, status_message = ?,
                    finished_at_utc = CASE WHEN ? THEN ? ELSE finished_at_utc END,
                    finished_at_local = CASE WHEN ? THEN ? ELSE finished_at_local END,
                    current_price_ron = COALESCE(?, current_price_ron),
                    availability = COALESCE(?, availability),
                    execution_delay_ms = COALESCE(?, execution_delay_ms),
                    review_json = COALESCE(?, review_json)
                   WHERE id = ?""",
                (
                    status,
                    message,
                    int(finished),
                    utc_stamp,
                    int(finished),
                    local_stamp,
                    current_price_ron,
                    availability,
                    execution_delay_ms,
                    json.dumps(review, ensure_ascii=False, default=str) if review else None,
                    run_id,
                ),
            )

    def add_event(
        self,
        run_id: str | None,
        level: str,
        event_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        utc_stamp, local_stamp = self._timestamps()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO events
                   (run_id, timestamp_utc, timestamp_local, level, event_type, message,
                    details_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    utc_stamp,
                    local_stamp,
                    level.upper(),
                    event_type,
                    message,
                    json.dumps(details or {}, ensure_ascii=False, default=str),
                ),
            )

    def latest_run(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs ORDER BY started_at_utc DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (max(1, limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_order_submission(self, idempotency_key: str, run_id: str) -> bool:
        """Atomically claim a one-time order attempt across app restarts."""
        utc_stamp, _ = self._timestamps()
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO order_intents
                   (idempotency_key, run_id, status, created_at_utc, updated_at_utc, message)
                   VALUES (?, ?, 'submission_started', ?, ?, ?)""",
                (
                    idempotency_key,
                    run_id,
                    utc_stamp,
                    utc_stamp,
                    "Final order action claimed; retries are blocked.",
                ),
            )
            return cursor.rowcount == 1

    def set_order_submission_status(
        self, idempotency_key: str, status: str, message: str
    ) -> None:
        utc_stamp, _ = self._timestamps()
        with self._connect() as connection:
            connection.execute(
                """UPDATE order_intents
                   SET status = ?, updated_at_utc = ?, message = ?
                   WHERE idempotency_key = ?""",
                (status, utc_stamp, message, idempotency_key),
            )

    def order_submission(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM order_intents WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return dict(row) if row else None
