from __future__ import annotations

import threading
import re
import hashlib
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any, Sequence

from playwright.sync_api import Error as PlaywrightError

from browser.emag import (
    HOME_URL,
    AuthenticationRequired,
    CaptchaRequired,
    CheckoutFlowBlocked,
    EmagPage,
    EmagWorkflowError,
)
from browser.manager import BrowserManager, DEFAULT_CDP_ENDPOINT, validate_cdp_endpoint
from core.checkout import AutomaticOrderBlocked, CheckoutPreparer
from core.conditions import ProductConfig
from core.monitor import monitor_until_ready
from core.multi_monitor import monitor_products
from core.scheduler import (
    ClockSyncError,
    SynchronizedClock,
    execution_delay_ms,
    wait_until,
)
from database.db import Database


class WorkerController:
    """Owns one cancellable workflow thread for the Streamlit dashboard."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._resume_event = threading.Event()
        self._status: dict[str, Any] = {
            "state": "idle",
            "message": "Ready",
            "run_id": None,
            "test_mode": False,
            "current_price_ron": None,
            "availability": "Unknown",
            "execution_delay_ms": None,
            "review": None,
            "products": [],
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        config: ProductConfig | Sequence[ProductConfig],
        *,
        test_mode: bool,
        profile_dir: str | Path,
        database_path: str | Path,
        ntp_servers: tuple[str, ...],
        browser_mode: str = "persistent",
        browser_cdp_endpoint: str = DEFAULT_CDP_ENDPOINT,
    ) -> str:
        configs = (config,) if isinstance(config, ProductConfig) else tuple(config)
        if not configs:
            raise ValueError("Configure at least one product.")
        if len(configs) > 10:
            raise ValueError("Configure no more than 10 products per monitoring run.")
        for item in configs:
            item.validate()
        if browser_mode not in {"persistent", "chrome_cdp"}:
            raise ValueError("Choose a supported browser mode.")
        if browser_mode == "chrome_cdp":
            browser_cdp_endpoint = validate_cdp_endpoint(browser_cdp_endpoint)
        codes = []
        for item in configs:
            match = re.search(r"/pd/([^/?#]+)", item.product_url, re.IGNORECASE)
            codes.append(match.group(1).casefold() if match else "")
        if len(set(codes)) != len(codes):
            raise ValueError("The same eMAG product cannot be monitored twice in one run.")
        if self.is_running():
            raise RuntimeError("A monitoring workflow is already running.")
        db = Database(database_path)
        run_config = (
            [asdict(item) for item in configs]
            if len(configs) > 1
            else asdict(configs[0])
        )
        run_id = db.start_run(run_config, test_mode)
        self._stop_event = threading.Event()
        self._resume_event = threading.Event()
        with self._lock:
            self._status = {
                "state": "starting",
                "message": "Starting the configured browser session.",
                "run_id": run_id,
                "test_mode": test_mode,
                "current_price_ron": None,
                "availability": "Unknown",
                "execution_delay_ms": None,
                "review": None,
                "products": [
                    {
                        "index": index,
                        "url": item.product_url,
                        "target": item.target_datetime().isoformat(),
                        "state": "scheduled",
                        "message": "Waiting for the product's target time.",
                        "current_price_ron": None,
                        "availability": "Unknown",
                        "execution_delay_ms": None,
                        "title": None,
                    }
                    for index, item in enumerate(configs)
                ],
            }
        worker = self._run_many if len(configs) > 1 else self._run
        worker_config = configs if len(configs) > 1 else configs[0]
        self._thread = threading.Thread(
            target=worker,
            args=(
                run_id,
                worker_config,
                test_mode,
                Path(profile_dir),
                Path(database_path),
                ntp_servers,
                browser_mode,
                browser_cdp_endpoint,
            ),
            name="emag-autobuy-worker",
            daemon=True,
        )
        db.add_event(
            run_id,
            "INFO",
            "run_started",
            "Workflow started in test mode." if test_mode else "Workflow started.",
            {
                "products": len(configs),
                "targets": [item.target_datetime().isoformat() for item in configs],
            },
        )
        self._thread.start()
        return run_id

    def stop(self) -> None:
        self._stop_event.set()
        self._resume_event.set()
        with self._lock:
            self._status["message"] = "Stopping the workflow."
            if self._status.get("state") not in {"idle", "stopped", "complete", "error"}:
                self._status["state"] = "stopping"

    def resume_after_manual_action(self) -> None:
        self._resume_event.set()

    def _update_local(self, **values: Any) -> None:
        with self._lock:
            self._status.update(values)

    def _report(
        self,
        db: Database,
        run_id: str,
        state: str,
        message: str,
        *,
        event_type: str | None = None,
        level: str = "INFO",
        details: dict[str, Any] | None = None,
        finished: bool = False,
        **run_values: Any,
    ) -> None:
        self._update_local(state=state, message=message, **run_values)
        db.set_run_status(run_id, state, message, finished=finished, **run_values)
        if event_type:
            db.add_event(run_id, level, event_type, message, details)

    def _wait_for_user(self, db: Database, run_id: str, state: str, message: str) -> bool:
        self._resume_event.clear()
        self._report(
            db,
            run_id,
            state,
            message,
            event_type="manual_action_required",
            level="WARNING",
        )
        while not self._stop_event.is_set():
            if self._resume_event.wait(timeout=0.25):
                self._resume_event.clear()
                return not self._stop_event.is_set()
        return False

    def _finish(
        self,
        db: Database,
        run_id: str,
        state: str,
        message: str,
        *,
        event_type: str,
        level: str = "INFO",
        details: dict[str, Any] | None = None,
        **run_values: Any,
    ) -> None:
        self._report(
            db,
            run_id,
            state,
            message,
            event_type=event_type,
            level=level,
            details=details,
            finished=True,
            **run_values,
        )

    def _hold_for_manual_order_review(
        self,
        db: Database,
        run_id: str,
        reason: str,
        *,
        review: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
        order_key: str | None = None,
    ) -> None:
        if order_key is not None:
            if not db.claim_order_submission(order_key, run_id):
                self._finish(
                    db,
                    run_id,
                    "duplicate_order_blocked",
                    "A previous automatic or manual review already exists for this scheduled basket. Check eMAG order history; the browser will not offer another submit path.",
                    event_type="duplicate_order_blocked",
                    level="ERROR",
                    details=details,
                    review=review,
                )
                return
            db.set_order_submission_status(
                order_key,
                "manual_review_opened",
                "The app stopped before its final click and left checkout open for manual review. A second run for this basket is blocked until its schedule changes.",
            )
        message = (
            f"Automatic order stopped before the final button: {reason} "
            "The eMAG browser remains open for your manual review. If you want to order, "
            "check the product, ticket recipient, fees, and total, then click the final "
            "button yourself. The app will not click it in this fallback. Press Stop in "
            "the dashboard when finished. This basket is locked against an automatic retry."
        )
        self._report(
            db,
            run_id,
            "awaiting_manual_order_review",
            message,
            event_type="manual_order_review_required",
            level="WARNING",
            details=details,
            review=review,
        )
        self._stop_event.wait()
        self._finish(
            db,
            run_id,
            "stopped_after_manual_order_review",
            "The browser review ended. The app did not submit or verify an order.",
            event_type="manual_order_review_ended",
        )

    @staticmethod
    def _order_submission_key(configs: Sequence[ProductConfig]) -> str:
        parts = []
        for config in configs:
            match = re.search(r"/pd/([^/?#]+)", config.product_url, re.IGNORECASE)
            if match is None:
                raise ValueError("A product code is required for duplicate-order protection.")
            parts.append(
                "|".join(
                    (
                        match.group(1).casefold(),
                        config.target_datetime().isoformat(),
                        str(config.quantity),
                    )
                )
            )
        return hashlib.sha256("||".join(sorted(parts)).encode("utf-8")).hexdigest()

    def _run_many(
        self,
        run_id: str,
        configs: Sequence[ProductConfig],
        test_mode: bool,
        profile_dir: Path,
        database_path: Path,
        ntp_servers: tuple[str, ...],
        browser_mode: str,
        browser_cdp_endpoint: str,
    ) -> None:
        """Monitor many products independently on one serialized browser page."""
        db = Database(database_path)
        try:
            self._report(
                db,
                run_id,
                "syncing_clock",
                "Checking the clock against NTP before scheduling products.",
                event_type="clock_sync_started",
            )
            clock = SynchronizedClock(ntp_servers)
            sync = clock.sync()
            db.add_event(
                run_id,
                "INFO",
                "clock_synchronized",
                "Clock offset measured; Windows system time was not changed.",
                {
                    "server": sync.server,
                    "offset_seconds": round(sync.offset_seconds, 6),
                    "checked_at_utc": sync.checked_at_utc.isoformat(),
                    "product_count": len(configs),
                },
            )
            with BrowserManager(
                profile_dir,
                mode=browser_mode,
                cdp_endpoint=browser_cdp_endpoint,
            ) as page:
                page.goto(HOME_URL, wait_until="domcontentloaded")
                emag = EmagPage(page)

                def log(
                    index: int,
                    level: str,
                    event_type: str,
                    message: str,
                    details: dict,
                ) -> None:
                    config = configs[index]
                    db.add_event(
                        run_id,
                        level,
                        event_type,
                        message,
                        {"product_index": index, "product_url": config.product_url, **details},
                    )

                def update_status(
                    index: int,
                    state: str,
                    message: str,
                    observation,
                    delay_ms: float | None,
                ) -> None:
                    product_status = {
                        "index": index,
                        "url": configs[index].product_url,
                        "target": configs[index].target_datetime().isoformat(),
                        "state": state,
                        "message": message,
                        "current_price_ron": (
                            str(observation.price_ron)
                            if observation and observation.price_ron is not None
                            else None
                        ),
                        "availability": (
                            "In stock"
                            if observation and observation.in_stock is True
                            else "Out of stock"
                            if observation and observation.in_stock is False
                            else "Unknown"
                        ),
                        "execution_delay_ms": delay_ms,
                        "title": observation.title if observation else None,
                    }
                    with self._lock:
                        products = list(self._status.get("products", []))
                        if index < len(products):
                            products[index] = product_status
                        self._status.update(
                            {
                                "products": products,
                                "state": "multi_monitoring",
                                "message": f"{sum(p['state'] in {'test_conditions_met', 'deadline_expired', 'stopped'} for p in products)}/{len(configs)} products finished.",
                                "current_price_ron": product_status["current_price_ron"],
                                "availability": product_status["availability"],
                                "execution_delay_ms": delay_ms,
                            }
                        )
                    db.set_run_status(
                        run_id,
                        "multi_monitoring",
                        f"Product {index + 1}: {message}",
                        current_price_ron=product_status["current_price_ron"],
                        availability=product_status["availability"].casefold().replace(" ", "_"),
                        execution_delay_ms=delay_ms,
                    )

                results = monitor_products(
                    emag,
                    configs,
                    clock=clock,
                    stop_event=self._stop_event,
                    resume_event=self._resume_event,
                    log=log,
                    on_status=update_status,
                )
                if not test_mode and not self._stop_event.is_set():
                    self._run_multi_live_purchase(
                        db, run_id, configs, results, emag
                    )
                    return
            if self._stop_event.is_set():
                state = "stopped"
                message = "Multi-product monitoring stopped by the user."
                event_type = "run_stopped"
            else:
                state = "test_completed"
                matched = sum(result.state == "test_conditions_met" for result in results)
                message = (
                    f"Test monitoring finished: {matched} of {len(results)} products met "
                    "their conditions. No cart or checkout action was performed."
                )
                event_type = "multi_product_test_complete"
            self._finish(
                db,
                run_id,
                state,
                message,
                event_type=event_type,
                details={
                    "products": [
                        {
                            "index": result.index,
                            "state": result.state,
                            "message": result.message,
                            "price_ron": (
                                str(result.observation.price_ron)
                                if result.observation
                                and result.observation.price_ron is not None
                                else None
                            ),
                        }
                        for result in results
                    ]
                },
            )
        except ClockSyncError as exc:
            self._finish(
                db,
                run_id,
                "clock_sync_error",
                str(exc),
                event_type="clock_sync_failed",
                level="ERROR",
            )
        except Exception as exc:
            self._finish(
                db,
                run_id,
                "error",
                f"Multi-product workflow stopped safely: {type(exc).__name__}: {exc}",
                event_type="workflow_error",
                level="ERROR",
                details={"error_type": type(exc).__name__},
            )

    def _run_multi_live_purchase(
        self,
        db: Database,
        run_id: str,
        configs: Sequence[ProductConfig],
        results,
        emag: EmagPage,
    ) -> None:
        ready = [
            result.state == "test_conditions_met" and result.observation is not None
            for result in results
        ]
        if not all(ready):
            missing = [
                index + 1 for index, is_ready in enumerate(ready) if not is_ready
            ]
            self._finish(
                db,
                run_id,
                "multi_order_not_ready",
                "The shared order was not prepared because every configured product must meet its conditions before the deadline. No products were added to the cart. "
                f"Products not ready: {missing}.",
                event_type="multi_order_not_ready",
                level="WARNING",
                details={"not_ready_product_indexes": missing},
            )
            return

        order_key = self._order_submission_key(tuple(configs))
        if db.order_submission(order_key) is not None:
            self._finish(
                db,
                run_id,
                "duplicate_order_blocked",
                "An automatic or manual order attempt already exists for this scheduled basket. Check eMAG order history; no products were added.",
                event_type="duplicate_order_blocked",
                level="ERROR",
                details={"product_count": len(configs)},
            )
            return

        products = tuple(result.observation for result in results)
        self._report(
            db,
            run_id,
            "preparing_shared_cart",
            f"All {len(configs)} products met their conditions; preparing one shared cart.",
            event_type="shared_cart_preparation_started",
            details={"product_count": len(configs)},
        )
        for index, config in enumerate(configs):
            db.add_event(
                run_id,
                "INFO",
                "shared_cart_item_started",
                "Rechecking this product and adding it to the shared cart once.",
                {
                    "product_index": index,
                    "product_url": config.product_url,
                    "quantity": config.quantity,
                },
            )

        preparer = CheckoutPreparer(emag)
        try:
            carts, prepared = preparer.prepare_multi(tuple(configs), products)
        except (EmagWorkflowError, PlaywrightError, RuntimeError) as exc:
            self._hold_for_manual_order_review(
                db,
                run_id,
                f"The shared cart or checkout could not be verified: {exc}",
                details={"product_count": len(configs)},
                order_key=order_key,
            )
            return

        item_rows = [
            {
                "product_title": item.product_title,
                "unit_price_ron": str(item.unit_price_ron),
                "quantity": item.quantity,
            }
            for item in prepared.items
        ]
        review = {
            "product_title": prepared.product_title,
            "items": item_rows,
            "unit_price_ron": None,
            "product_subtotal_ron": str(prepared.unit_price_ron),
            "quantity": prepared.quantity,
            "cart_total_ron": str(prepared.cart_total_ron),
            "payment_options": list(prepared.payment_options),
            "checkout_url": prepared.checkout_url,
            "digital_delivery_confirmed": prepared.digital_delivery_confirmed,
            "product_count": len(prepared.items),
        }
        self._update_local(review=review)
        if self._stop_event.is_set():
            self._finish(
                db,
                run_id,
                "stopped",
                "Workflow stopped before the shared order step.",
                event_type="run_stopped_before_order",
                review=review,
            )
            return

        expected_titles = tuple(item.product_title for item in prepared.items)
        try:
            CheckoutPreparer.validate_automatic_order(prepared)
            emag.validate_order_submission(
                prepared.cart_total_ron,
                expected_product_subtotal_ron=prepared.product_subtotal_ron,
                expected_product_titles=expected_titles,
            )
        except (AutomaticOrderBlocked, EmagWorkflowError, PlaywrightError, RuntimeError) as exc:
            self._hold_for_manual_order_review(
                db,
                run_id,
                str(exc),
                review=review,
                details={
                    "product_count": len(configs),
                    "cart_total_ron": str(prepared.cart_total_ron),
                },
                order_key=order_key,
            )
            return
        if not db.claim_order_submission(order_key, run_id):
            self._finish(
                db,
                run_id,
                "duplicate_order_blocked",
                "A shared order attempt already exists for these products, targets, and quantities. No second order was sent.",
                event_type="duplicate_order_blocked",
                level="ERROR",
                details={"product_count": len(configs)},
                review=review,
            )
            return

        db.add_event(
            run_id,
            "WARNING",
            "shared_order_submission_started",
            "One shared final order action is being sent after the combined total was rechecked against the 100 RON limit.",
            {
                "product_count": len(configs),
                "products": item_rows,
                "cart_total_ron": str(prepared.cart_total_ron),
                "maximum_total_ron": "100.00",
            },
        )
        try:
            submission = preparer.submit_automatic_order(
                prepared,
                stop_event=self._stop_event,
                expected_product_titles=expected_titles,
            )
        except Exception as exc:
            db.set_order_submission_status(
                order_key,
                "order_status_unknown",
                f"Shared order result is uncertain ({type(exc).__name__}); automatic retry is disabled.",
            )
            self._report(
                db,
                run_id,
                "order_status_unknown",
                "The shared order action may have reached eMAG, but its result could not be verified. Do not retry; check eMAG order history manually.",
                event_type="order_status_unknown",
                level="ERROR",
                details={"cart_total_ron": str(prepared.cart_total_ron)},
                review=review,
            )
            self._stop_event.wait()
            return

        db.set_order_submission_status(order_key, submission.status, submission.message)
        if submission.status == "cancelled_before_click":
            self._finish(
                db,
                run_id,
                "stopped",
                submission.message,
                event_type="run_stopped_before_order_click",
                review=review,
            )
            return
        if submission.status == "order_placed_verified":
            self._finish(
                db,
                run_id,
                submission.status,
                submission.message,
                event_type=submission.status,
                details={"cart_total_ron": str(prepared.cart_total_ron)},
                review=review,
            )
            return

        self._report(
            db,
            run_id,
            "manual_payment_required"
            if submission.status == "manual_payment_or_security_step"
            else "order_status_unknown",
            submission.message,
            event_type=submission.status,
            level="WARNING",
            details={"cart_total_ron": str(prepared.cart_total_ron)},
            review=review,
        )
        self._stop_event.wait()
        self._finish(
            db,
            run_id,
            "stopped_after_order_review",
            "Shared order review ended. The app did not retry the order action.",
            event_type="run_stopped",
            review=review,
        )

    def _run(
        self,
        run_id: str,
        config: ProductConfig,
        test_mode: bool,
        profile_dir: Path,
        database_path: Path,
        ntp_servers: tuple[str, ...],
        browser_mode: str,
        browser_cdp_endpoint: str,
    ) -> None:
        db = Database(database_path)
        try:
            self._report(
                db,
                run_id,
                "syncing_clock",
                "Checking the clock against NTP before scheduling.",
                event_type="clock_sync_started",
            )
            clock = SynchronizedClock(ntp_servers)
            sync = clock.sync()
            db.add_event(
                run_id,
                "INFO",
                "clock_synchronized",
                "Clock offset measured; Windows system time was not changed.",
                {
                    "server": sync.server,
                    "offset_seconds": round(sync.offset_seconds, 6),
                    "checked_at_utc": sync.checked_at_utc.isoformat(),
                },
            )
            target = config.target_datetime()
            deadline = target + timedelta(minutes=config.deadline_minutes_after_target)
            now = clock.now(target.tzinfo)
            if now >= deadline:
                self._finish(
                    db,
                    run_id,
                    "deadline_expired",
                    "The scheduled monitoring window has already expired.",
                    event_type="deadline_expired",
                    level="WARNING",
                    details={
                        "target": target.isoformat(),
                        "now": now.isoformat(),
                        "deadline": deadline.isoformat(),
                    },
                )
                return

            with BrowserManager(
                profile_dir,
                mode=browser_mode,
                cdp_endpoint=browser_cdp_endpoint,
            ) as page:
                # The user can sign in manually while the scheduler waits.
                page.goto(HOME_URL, wait_until="domcontentloaded")
                if now < target:
                    last_bucket = [-1]
                    latest_sync = [sync]
                    resync_done = [False]

                    def report_wait(current, remaining: float) -> None:
                        if remaining <= 60 and not resync_done[0]:
                            refreshed = clock.sync()
                            latest_sync[0] = refreshed
                            resync_done[0] = True
                            db.add_event(
                                run_id,
                                "INFO",
                                "clock_resynchronized",
                                "Clock offset refreshed shortly before execution.",
                                {
                                    "server": refreshed.server,
                                    "offset_seconds": round(refreshed.offset_seconds, 6),
                                    "checked_at_utc": refreshed.checked_at_utc.isoformat(),
                                },
                            )
                        seconds = int(remaining)
                        self._update_local(
                            state="scheduled",
                            message=f"Waiting for target time ({seconds}s remaining).",
                        )
                        bucket = seconds // 30
                        if bucket != last_bucket[0]:
                            last_bucket[0] = bucket
                            db.add_event(
                                run_id,
                                "INFO",
                                "scheduler_wait",
                                "Waiting for the configured target time.",
                                {
                                    "current_time": current.isoformat(),
                                    "target_time": target.isoformat(),
                                    "seconds_remaining": seconds,
                                },
                            )

                    scheduled = wait_until(
                        target,
                        clock,
                        self._stop_event,
                        on_wait=report_wait,
                    )
                    if scheduled is None:
                        self._finish(
                            db,
                            run_id,
                            "stopped",
                            "Workflow stopped before the target time.",
                            event_type="run_stopped",
                        )
                        return
                    sync = latest_sync[0]

                actual = clock.now(target.tzinfo)
                delay = execution_delay_ms(target, actual)
                delay_message = f"Scheduled execution began {delay:.0f} ms after target time."
                self._report(
                    db,
                    run_id,
                    "monitoring",
                    "Checking the visible product page.",
                    event_type="scheduled_execution",
                    details={
                        "target_time": target.isoformat(),
                        "actual_time": actual.isoformat(),
                        "execution_delay_ms": delay,
                        "clock_offset_seconds": sync.offset_seconds,
                    },
                    execution_delay_ms=delay,
                )
                db.add_event(run_id, "INFO", "execution_delay", delay_message, {"delay_ms": delay})

                def log(level: str, event_type: str, message: str, details: dict) -> None:
                    db.add_event(run_id, level, event_type, message, details)

                def update_observation(observation) -> None:
                    product_state = {
                        "index": 0,
                        "url": config.product_url,
                        "target": config.target_datetime().isoformat(),
                        "state": "monitoring",
                        "message": "Checking product availability and price.",
                        "current_price_ron": str(observation.price_ron)
                        if observation.price_ron is not None
                        else None,
                        "availability": (
                            "In stock"
                            if observation.in_stock is True
                            else "Out of stock"
                            if observation.in_stock is False
                            else "Unknown"
                        ),
                        "execution_delay_ms": delay,
                        "title": observation.title,
                    }
                    self._update_local(
                        current_price_ron=str(observation.price_ron)
                        if observation.price_ron is not None
                        else None,
                        availability=(
                            "In stock"
                            if observation.in_stock is True
                            else "Out of stock"
                            if observation.in_stock is False
                            else "Unknown"
                        ),
                        products=[product_state],
                    )
                    db.set_run_status(
                        run_id,
                        "monitoring",
                        "Checking product availability and price.",
                        current_price_ron=str(observation.price_ron)
                        if observation.price_ron is not None
                        else None,
                        availability=(
                            "in_stock"
                            if observation.in_stock is True
                            else "out_of_stock"
                            if observation.in_stock is False
                            else "unknown"
                        ),
                    )

                emag = EmagPage(page)
                observation = monitor_until_ready(
                    emag,
                    config,
                    deadline=deadline,
                    clock=clock,
                    stop_event=self._stop_event,
                    log=log,
                    on_observation=update_observation,
                )
                if self._stop_event.is_set():
                    self._finish(
                        db,
                        run_id,
                        "stopped",
                        "Workflow stopped by the user.",
                        event_type="run_stopped",
                    )
                    return
                if observation is None:
                    self._finish(
                        db,
                        run_id,
                        "deadline_expired",
                        "No qualifying product was found before the monitoring deadline.",
                        event_type="deadline_expired",
                        level="WARNING",
                    )
                    return
                if test_mode:
                    self._update_local(
                        products=[
                            {
                                "index": 0,
                                "url": config.product_url,
                                "target": config.target_datetime().isoformat(),
                                "state": "test_conditions_met",
                                "message": "Product conditions met in test mode.",
                                "current_price_ron": str(observation.price_ron),
                                "availability": "In stock"
                                if observation.in_stock is True
                                else "Out of stock"
                                if observation.in_stock is False
                                else "Unknown",
                                "execution_delay_ms": delay,
                                "title": observation.title,
                            }
                        ]
                    )
                    self._finish(
                        db,
                        run_id,
                        "test_conditions_met",
                        "Test mode passed: the product met the configured conditions. "
                        "No cart or checkout action was performed.",
                        event_type="test_mode_complete",
                        current_price_ron=str(observation.price_ron),
                        availability=(
                            "in_stock"
                            if observation.in_stock is True
                            else "out_of_stock"
                            if observation.in_stock is False
                            else "unknown"
                        ),
                    )
                    return

                order_key = self._order_submission_key((config,))
                if db.order_submission(order_key) is not None:
                    self._finish(
                        db,
                        run_id,
                        "duplicate_order_blocked",
                        "An automatic or manual order attempt already exists for this scheduled basket. Check eMAG order history; no cart action was taken.",
                        event_type="duplicate_order_blocked",
                        level="ERROR",
                    )
                    return

                preparer = CheckoutPreparer(emag)
                for attempt in range(1, config.max_attempts + 1):
                    if self._stop_event.is_set():
                        break
                    self._report(
                        db,
                        run_id,
                        "preparing_cart",
                        f"Preparing and verifying the cart (attempt {attempt}/{config.max_attempts}).",
                        event_type="cart_attempt",
                        details={"attempt": attempt},
                    )
                    try:
                        cart, prepared = preparer.prepare(config, observation)
                    except CheckoutFlowBlocked as exc:
                        self._hold_for_manual_order_review(
                            db,
                            run_id,
                            f"Digital delivery or checkout could not be verified: {exc}",
                            details={"attempt": attempt},
                            order_key=order_key,
                        )
                        return
                    except AuthenticationRequired as exc:
                        if not self._wait_for_user(
                            db,
                            run_id,
                            "awaiting_manual_login",
                            f"{exc} Resume the workflow after sign-in.",
                        ):
                            break
                        # Recheck price and stock after a session change.
                        observation = emag.read_product(config.product_url)
                        continue
                    except CaptchaRequired as exc:
                        if not self._wait_for_user(
                            db,
                            run_id,
                            "awaiting_manual_challenge",
                            f"{exc} Resume after handling it in the browser.",
                        ):
                            break
                        observation = emag.read_product(config.product_url)
                        continue
                    except (EmagWorkflowError, PlaywrightError, RuntimeError) as exc:
                        db.add_event(
                            run_id,
                            "WARNING",
                            "cart_attempt_failed",
                            str(exc),
                            {"attempt": attempt, "error_type": type(exc).__name__},
                        )
                        if attempt < config.max_attempts:
                            if self._stop_event.wait(config.poll_interval_seconds):
                                break
                            observation = emag.read_product(config.product_url)
                            continue
                        self._finish(
                            db,
                            run_id,
                            "cart_not_ready",
                            "The cart could not be safely prepared within the attempt limit.",
                            event_type="cart_preparation_failed",
                            level="ERROR",
                        )
                        return
                    review = {
                        "product_title": prepared.product_title,
                        "unit_price_ron": str(prepared.unit_price_ron),
                        "product_subtotal_ron": (
                            str(prepared.product_subtotal_ron)
                            if prepared.product_subtotal_ron is not None
                            else None
                        ),
                        "quantity": prepared.quantity,
                        "cart_total_ron": str(prepared.cart_total_ron),
                        "payment_options": list(prepared.payment_options),
                        "checkout_url": prepared.checkout_url,
                        "cart_quantity": cart.quantity,
                        "digital_delivery_confirmed": prepared.digital_delivery_confirmed,
                    }
                    if self._stop_event.is_set():
                        self._finish(
                            db,
                            run_id,
                            "stopped",
                            "Workflow stopped before the final order step.",
                            event_type="run_stopped_before_order",
                        )
                        return
                    try:
                        CheckoutPreparer.validate_automatic_order(prepared)
                        emag.validate_order_submission(
                            prepared.cart_total_ron,
                            expected_product_subtotal_ron=prepared.product_subtotal_ron,
                            expected_product_titles=(prepared.product_title,),
                        )
                    except (AutomaticOrderBlocked, EmagWorkflowError, PlaywrightError, RuntimeError) as exc:
                        self._update_local(
                            products=[
                                {
                                    "index": 0,
                                    "url": config.product_url,
                                    "target": config.target_datetime().isoformat(),
                                    "state": "awaiting_manual_order_review",
                                    "message": str(exc),
                                    "current_price_ron": str(prepared.unit_price_ron),
                                    "availability": "In stock",
                                    "execution_delay_ms": delay,
                                    "title": prepared.product_title,
                                }
                            ]
                        )
                        self._hold_for_manual_order_review(
                            db,
                            run_id,
                            str(exc),
                            review=review,
                            details={"cart_total_ron": str(prepared.cart_total_ron)},
                            order_key=order_key,
                        )
                        return

                    product_code_match = re.search(
                        r"/pd/([^/?#]+)", config.product_url, re.IGNORECASE
                    )
                    if not db.claim_order_submission(order_key, run_id):
                        self._finish(
                            db,
                            run_id,
                            "duplicate_order_blocked",
                            "An order attempt already exists for this product, schedule, and quantity. No second order was sent.",
                            event_type="duplicate_order_blocked",
                            level="ERROR",
                            details={"cart_total_ron": str(prepared.cart_total_ron)},
                        )
                        return
                    db.add_event(
                        run_id,
                        "WARNING",
                        "order_submission_started",
                        "One final order action is being sent after the checkout total and 100 RON limit were rechecked.",
                        {
                            "product_code": product_code_match.group(1)
                            if product_code_match
                            else None,
                            "cart_total_ron": str(prepared.cart_total_ron),
                            "maximum_total_ron": "100.00",
                        },
                    )
                    try:
                        submission = preparer.submit_automatic_order(
                            prepared,
                            stop_event=self._stop_event,
                            expected_product_titles=(prepared.product_title,),
                        )
                    except Exception as exc:
                        db.set_order_submission_status(
                            order_key,
                            "order_status_unknown",
                            f"Final order action outcome is uncertain ({type(exc).__name__}); automatic retry is disabled.",
                        )
                        self._finish(
                            db,
                            run_id,
                            "order_status_unknown",
                            "The order action may have reached eMAG, but its result could not be verified. Do not retry; check eMAG order history manually.",
                            event_type="order_status_unknown",
                            level="ERROR",
                            details={"cart_total_ron": str(prepared.cart_total_ron)},
                        )
                        self._stop_event.wait()
                        return
                    db.set_order_submission_status(
                        order_key, submission.status, submission.message
                    )
                    if submission.status == "cancelled_before_click":
                        self._finish(
                            db,
                            run_id,
                            "stopped",
                            submission.message,
                            event_type="run_stopped_before_order_click",
                            details={"cart_total_ron": str(prepared.cart_total_ron)},
                        )
                        return
                    self._update_local(
                        products=[
                            {
                                "index": 0,
                                "url": config.product_url,
                                "target": config.target_datetime().isoformat(),
                                "state": submission.status,
                                "message": submission.message,
                                "current_price_ron": str(prepared.unit_price_ron),
                                "availability": "In stock",
                                "execution_delay_ms": delay,
                                "title": prepared.product_title,
                            }
                        ]
                    )
                    if submission.status == "order_placed_verified":
                        self._finish(
                            db,
                            run_id,
                            "order_placed_verified",
                            submission.message,
                            event_type="order_placed_verified",
                            details={"cart_total_ron": str(prepared.cart_total_ron)},
                            review=review,
                        )
                        return
                    final_state = (
                        "manual_payment_required"
                        if submission.status == "manual_payment_or_security_step"
                        else "order_status_unknown"
                    )
                    self._report(
                        db,
                        run_id,
                        final_state,
                        submission.message,
                        event_type=submission.status,
                        level="WARNING",
                        details={"cart_total_ron": str(prepared.cart_total_ron)},
                        review=review,
                    )
                    # Keep the visible page open for manual 3-D Secure or status verification.
                    self._stop_event.wait()
                    self._finish(
                        db,
                        run_id,
                        "stopped_after_order_review",
                        "Workflow stopped after manual review. The app did not retry the order action.",
                        event_type="run_stopped",
                    )
                    return

                if self._stop_event.is_set():
                    self._finish(
                        db,
                        run_id,
                        "stopped",
                        "Workflow stopped by the user.",
                        event_type="run_stopped",
                    )
        except ClockSyncError as exc:
            self._finish(
                db,
                run_id,
                "clock_sync_error",
                str(exc),
                event_type="clock_sync_failed",
                level="ERROR",
            )
        except Exception as exc:
            self._finish(
                db,
                run_id,
                "error",
                f"Workflow stopped safely: {type(exc).__name__}: {exc}",
                event_type="workflow_error",
                level="ERROR",
                details={"error_type": type(exc).__name__},
            )
