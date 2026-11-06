from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Callable, Iterable


class ClockSyncError(RuntimeError):
    """NTP time could not be checked before the scheduled run."""


@dataclass(frozen=True)
class ClockSync:
    server: str
    offset_seconds: float
    checked_at_utc: datetime


class SynchronizedClock:
    """NTP-corrected clock view; it never changes the Windows system clock."""

    def __init__(self, servers: Iterable[str], timeout_seconds: float = 2.0) -> None:
        self.servers = tuple(servers)
        self.timeout_seconds = timeout_seconds
        self.sync_result: ClockSync | None = None
        self._anchor_utc: datetime | None = None
        self._anchor_monotonic: float | None = None

    def sync(self) -> ClockSync:
        try:
            import ntplib
        except ImportError as exc:
            raise ClockSyncError("ntplib is not installed; install requirements.txt.") from exc

        failures: list[str] = []
        for server in self.servers:
            try:
                response = ntplib.NTPClient().request(
                    server, version=3, timeout=self.timeout_seconds
                )
                result = ClockSync(
                    server=server,
                    offset_seconds=float(response.offset),
                    checked_at_utc=datetime.now(timezone.utc),
                )
                self.sync_result = result
                self._anchor_utc = datetime.now(timezone.utc)
                self._anchor_monotonic = time.monotonic()
                return result
            except Exception as exc:  # network and NTP library errors
                failures.append(f"{server}: {type(exc).__name__}")
        raise ClockSyncError(
            "Could not synchronize with any configured NTP server. "
            + "; ".join(failures)
        )

    def now(self, zone: tzinfo | None = None) -> datetime:
        if (
            self.sync_result is not None
            and self._anchor_utc is not None
            and self._anchor_monotonic is not None
        ):
            elapsed = time.monotonic() - self._anchor_monotonic
            current = self._anchor_utc + timedelta(
                seconds=elapsed + self.sync_result.offset_seconds
            )
        else:
            current = datetime.now(timezone.utc)
        return current.astimezone(zone) if zone else current


@dataclass(frozen=True)
class ScheduledStart:
    target: datetime
    actual: datetime
    delay_ms: float


def wait_until(
    target: datetime,
    clock: SynchronizedClock,
    stop_event: threading.Event,
    *,
    on_wait: Callable[[datetime, float], None] | None = None,
    precision_seconds: float = 0.25,
) -> ScheduledStart | None:
    """Wait until the synchronized clock reaches target; never starts early."""
    if target.tzinfo is None:
        raise ValueError("Scheduled target must be timezone-aware.")
    while True:
        now = clock.now(target.tzinfo)
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            return ScheduledStart(target, now, max(0.0, -remaining * 1000.0))
        if on_wait is not None:
            on_wait(now, remaining)
        if stop_event.wait(min(remaining, precision_seconds)):
            return None


def execution_delay_ms(target: datetime, actual: datetime) -> float:
    if target.tzinfo is None or actual.tzinfo is None:
        raise ValueError("Target and actual timestamps must be timezone-aware.")
    return max(0.0, (actual - target).total_seconds() * 1000.0)
