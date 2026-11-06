from __future__ import annotations

import threading
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol, Sequence

from browser.emag import (
    AuthenticationRequired,
    CaptchaRequired,
    EmagWorkflowError,
    PlaywrightError,
    PlaywrightTimeoutError,
)
from core.conditions import (
    ProductConfig,
    ProductObservation,
    evaluate_product_conditions,
)


class MultiMonitorClock(Protocol):
    def now(self, zone=None) -> datetime: ...

    def sync(self): ...


@dataclass
class _Job:
    index: int
    config: ProductConfig
    target: datetime
    deadline: datetime
    next_check: datetime
    state: str = "scheduled"
    message: str = "Waiting for the product's target time."
    observation: ProductObservation | None = None
    execution_delay_ms: float | None = None
    started: bool = False
    clock_resynchronized: bool = False
    waiting_manual: bool = False


@dataclass(frozen=True)
class ProductMonitorResult:
    index: int
    state: str
    message: str
    observation: ProductObservation | None
    execution_delay_ms: float | None


LogCallback = Callable[[int, str, str, str, dict], None]
StatusCallback = Callable[
    [int, str, str, ProductObservation | None, float | None], None
]


def monitor_products(
    emag,
    products: Sequence[ProductConfig],
    *,
    clock: MultiMonitorClock,
    stop_event: threading.Event,
    resume_event: threading.Event,
    log: LogCallback,
    on_status: StatusCallback,
) -> list[ProductMonitorResult]:
    """Multiplex independent schedules through one Playwright page.

    Each product has its own target, polling interval, deadline, result and event
    history. Browser requests run serially on one Playwright thread so page state
    and the persistent profile cannot race.
    """
    if not products:
        raise ValueError("At least one product is required.")
    jobs: list[_Job] = []
    product_codes: set[str] = set()
    for index, config in enumerate(products):
        config.validate()
        match = re.search(r"/pd/([^/?#]+)", config.product_url, re.IGNORECASE)
        code = match.group(1).casefold() if match else ""
        if not code or code in product_codes:
            raise ValueError("Each monitored product must have a unique eMAG product code.")
        product_codes.add(code)
        target = config.target_datetime()
        jobs.append(
            _Job(
                index=index,
                config=config,
                target=target,
                deadline=target + timedelta(
                    minutes=config.deadline_minutes_after_target
                ),
                next_check=target,
            )
        )
        on_status(index, "scheduled", "Waiting for the product's target time.", None, None)

    completed: set[int] = set()
    last_browser_action = 0.0
    while len(completed) < len(jobs):
        if resume_event.is_set():
            resume_event.clear()
            now = clock.now(jobs[0].target.tzinfo)
            for job in jobs:
                if job.waiting_manual:
                    job.waiting_manual = False
                    job.next_check = now
                    job.state = "monitoring"
                    job.message = "Manual browser step completed; rechecking this product."
                    log(
                        job.index,
                        "INFO",
                        "manual_action_resumed",
                        job.message,
                        {},
                    )
                    on_status(
                        job.index,
                        job.state,
                        job.message,
                        job.observation,
                        job.execution_delay_ms,
                    )
        if stop_event.is_set():
            for job in jobs:
                if job.index in completed:
                    continue
                job.state = "stopped"
                job.message = "Monitoring stopped by the user."
                log(job.index, "WARNING", "run_stopped", job.message, {})
                on_status(
                    job.index,
                    job.state,
                    job.message,
                    job.observation,
                    job.execution_delay_ms,
                )
                completed.add(job.index)
            break

        now = clock.now(jobs[0].target.tzinfo)
        for job in jobs:
            if job.index in completed:
                continue
            if now >= job.deadline:
                job.state = "deadline_expired"
                job.message = "No qualifying product was found before its monitoring deadline."
                log(job.index, "WARNING", "monitor_deadline", job.message, {})
                on_status(
                    job.index,
                    job.state,
                    job.message,
                    job.observation,
                    job.execution_delay_ms,
                )
                completed.add(job.index)

        if len(completed) == len(jobs):
            break

        near_targets = [
            job
            for job in jobs
            if job.index not in completed
            and not job.clock_resynchronized
            and now < job.target
            and (job.target - now).total_seconds() <= 60
        ]
        if near_targets:
            sync = clock.sync()
            for job in near_targets:
                job.clock_resynchronized = True
                log(
                    job.index,
                    "INFO",
                    "clock_resynchronized",
                    "Clock offset refreshed shortly before this product's target time.",
                    {
                        "server": sync.server,
                        "offset_seconds": round(sync.offset_seconds, 6),
                        "checked_at_utc": sync.checked_at_utc.isoformat(),
                    },
                )
            now = clock.now(jobs[0].target.tzinfo)

        manual_gate = any(
            job.waiting_manual for job in jobs if job.index not in completed
        )
        due = [
            job
            for job in jobs
            if job.index not in completed
            and now >= job.target
            and now >= job.next_check
            and not manual_gate
        ]
        due.sort(key=lambda job: (job.next_check, job.index))
        for job in due:
            if stop_event.is_set():
                break
            if any(
                job.waiting_manual for job in jobs if job.index not in completed
            ):
                break
            spacing = 1.0 - (time.monotonic() - last_browser_action)
            if last_browser_action and spacing > 0 and stop_event.wait(spacing):
                break
            actual = clock.now(job.target.tzinfo)
            if actual >= job.deadline:
                job.state = "deadline_expired"
                job.message = "No qualifying product was found before its monitoring deadline."
                log(job.index, "WARNING", "monitor_deadline", job.message, {})
                on_status(
                    job.index,
                    job.state,
                    job.message,
                    job.observation,
                    job.execution_delay_ms,
                )
                completed.add(job.index)
                continue
            if actual < job.target:
                # A clock resynchronization can move the corrected time backward.
                continue
            last_browser_action = time.monotonic()
            if not job.started:
                job.started = True
                job.execution_delay_ms = max(
                    0.0, (actual - job.target).total_seconds() * 1000
                )
                log(
                    job.index,
                    "INFO",
                    "scheduled_execution",
                    "Product monitoring began at or after its configured target time.",
                    {
                        "target_time": job.target.isoformat(),
                        "actual_time": actual.isoformat(),
                        "execution_delay_ms": job.execution_delay_ms,
                    },
                )
            job.state = "monitoring"
            job.message = "Checking this product's visible price and availability."
            on_status(
                job.index,
                job.state,
                job.message,
                job.observation,
                job.execution_delay_ms,
            )
            try:
                emag.open_product(job.config.product_url)
                observation = emag.read_product()
                job.observation = observation
                conditions = evaluate_product_conditions(observation, job.config)
                details = {
                    "title": observation.title,
                    "price_ron": (
                        str(observation.price_ron)
                        if observation.price_ron is not None
                        else None
                    ),
                    "availability": observation.in_stock,
                    "can_purchase": observation.can_purchase,
                    "reasons": list(conditions.reasons),
                    "observed_at_utc": observation.observed_at.isoformat(),
                }
                log(
                    job.index,
                    "INFO" if conditions.ready else "DEBUG",
                    "product_check",
                    "Product conditions met."
                    if conditions.ready
                    else "Product conditions are not met.",
                    details,
                )
                if conditions.ready:
                    job.state = "test_conditions_met"
                    job.message = "This product met its configured conditions."
                    on_status(
                        job.index,
                        job.state,
                        job.message,
                        observation,
                        job.execution_delay_ms,
                    )
                    completed.add(job.index)
                    continue
            except AuthenticationRequired as exc:
                job.state = "awaiting_manual_login"
                job.message = str(exc)
                job.waiting_manual = True
                log(job.index, "WARNING", "manual_login_required", job.message, {})
                on_status(
                    job.index,
                    job.state,
                    job.message,
                    job.observation,
                    job.execution_delay_ms,
                )
            except CaptchaRequired as exc:
                job.state = "awaiting_manual_challenge"
                job.message = str(exc)
                job.waiting_manual = True
                log(job.index, "WARNING", "manual_challenge_required", job.message, {})
                on_status(
                    job.index,
                    job.state,
                    job.message,
                    job.observation,
                    job.execution_delay_ms,
                )
            except EmagWorkflowError as exc:
                job.state = "monitoring"
                job.message = str(exc)
                log(job.index, "WARNING", "page_not_ready", job.message, {})
                on_status(
                    job.index,
                    job.state,
                    job.message,
                    job.observation,
                    job.execution_delay_ms,
                )
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                job.state = "monitoring"
                job.message = "Network or browser error; this product will be retried."
                log(
                    job.index,
                    "WARNING",
                    "browser_retry",
                    job.message,
                    {"error_type": type(exc).__name__},
                )
                on_status(
                    job.index,
                    job.state,
                    job.message,
                    job.observation,
                    job.execution_delay_ms,
                )
            if job.index not in completed:
                job.next_check = clock.now(job.target.tzinfo) + timedelta(
                    seconds=job.config.poll_interval_seconds
                )

        if len(completed) < len(jobs):
            now = clock.now(jobs[0].target.tzinfo)
            next_times = [
                (
                    job.deadline
                    if job.waiting_manual
                    else min(job.deadline, max(job.target, job.next_check))
                )
                for job in jobs
                if job.index not in completed
            ]
            seconds = (
                0.25
                if any(job.waiting_manual for job in jobs if job.index not in completed)
                else max(0.01, (min(next_times) - now).total_seconds())
            )
            stop_event.wait(min(seconds, 1.0))

    return [
        ProductMonitorResult(
            index=job.index,
            state=(job.state if job.index in completed else "stopped"),
            message=job.message,
            observation=job.observation,
            execution_delay_ms=job.execution_delay_ms,
        )
        for job in jobs
    ]
