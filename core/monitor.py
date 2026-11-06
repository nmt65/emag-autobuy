from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable, Protocol

from browser.emag import (
    EmagPage,
    EmagWorkflowError,
    PlaywrightError,
    PlaywrightTimeoutError,
)
from core.conditions import ProductConfig, ProductObservation, evaluate_product_conditions


class ClockView(Protocol):
    def now(self, zone=None) -> datetime: ...


def monitor_until_ready(
    emag: EmagPage,
    config: ProductConfig,
    *,
    deadline: datetime,
    clock: ClockView,
    stop_event: threading.Event,
    log: Callable[[str, str, dict], None],
    on_observation: Callable[[ProductObservation], None] | None = None,
) -> ProductObservation | None:
    """Poll visible product data at a conservative interval until ready/deadline."""
    opened = False
    while not stop_event.is_set():
        now = clock.now(deadline.tzinfo)
        if now >= deadline:
            log("WARNING", "monitor_deadline", "Monitoring deadline expired.", {})
            return None
        try:
            if not opened:
                emag.open_product(config.product_url)
                opened = True
            else:
                emag.refresh_product()
            observation = emag.read_product()
            conditions = evaluate_product_conditions(observation, config)
            if on_observation is not None:
                on_observation(observation)
            log(
                "INFO" if conditions.ready else "DEBUG",
                "product_check",
                "Product conditions met."
                if conditions.ready
                else "Product conditions are not met.",
                {
                    "title": observation.title,
                    "price_ron": str(observation.price_ron)
                    if observation.price_ron is not None
                    else None,
                    "availability": observation.in_stock,
                    "can_purchase": observation.can_purchase,
                    "reasons": list(conditions.reasons),
                    "observed_at_utc": observation.observed_at.isoformat(),
                },
            )
            if conditions.ready:
                return observation
        except EmagWorkflowError:
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            opened = False
            log(
                "WARNING",
                "browser_retry",
                "A browser timeout or network error occurred; monitoring will retry.",
                {"error_type": type(exc).__name__},
            )

        remaining = max(0.0, (deadline - clock.now(deadline.tzinfo)).total_seconds())
        if remaining <= 0:
            continue
        stop_event.wait(min(config.poll_interval_seconds, remaining))
    return None
