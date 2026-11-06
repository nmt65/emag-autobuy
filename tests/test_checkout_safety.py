import ast
import threading
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from browser.emag import CartSnapshot, CheckoutReview, EmagPage
from core.checkout import (
    AutomaticOrderBlocked,
    CheckoutPreparer,
    PreparedPurchase,
)
from core.conditions import ProductConfig, ProductObservation
from core.monitor import monitor_until_ready


class FixedClock:
    def __init__(self, current):
        self.current = current

    def now(self, zone=None):
        return self.current.astimezone(zone) if zone else self.current


class ReadyProductPage:
    def __init__(self, observation):
        self.observation = observation

    def open_product(self, _url):
        pass

    def read_product(self):
        return self.observation


class ReviewOnlyCart:
    def __init__(self):
        self.calls = []

    def prepare_cart(self, _config, _product):
        self.calls.append("prepare_cart")
        return CartSnapshot(
            visible_text="Sample product; Cost produse: 100,00 Lei; Total: 112,00 Lei",
            product_present=True,
            cart_unit_price_ron=Decimal("100.00"),
            quantity_field=None,
            quantity=1,
            cart_total_ron=Decimal("112.00"),
            product_subtotal_ron=Decimal("100.00"),
            cart_known_empty=False,
            ambiguous_product_reference=False,
            quantity_control_count=1,
        )

    def proceed_to_checkout(self, config, product):
        self.calls.append("proceed_to_checkout")
        return CheckoutReview(
            product_title=product.title,
            quantity=config.quantity,
            cart_total_ron=Decimal("112.00"),
            payment_options=("Card online", "Ramburs"),
            checkout_url="https://www.emag.ro/cart/checkout",
            product_subtotal_ron=Decimal("100.00"),
            digital_delivery_confirmed=True,
        )


class CheckoutSafetyTests(unittest.TestCase):
    def test_monitor_to_checkout_review_completes_without_order_submission(self):
        zone = ZoneInfo("Europe/Bucharest")
        now = datetime.now(zone)
        config = ProductConfig(
            product_url="https://www.emag.ro/item/pd/AB12CD3BM/",
            max_price_ron=Decimal("100.00"),
            quantity=1,
            target_date=now.date().isoformat(),
            target_time=(now + timedelta(minutes=1)).strftime("%H:%M:%S"),
            max_attempts=1,
            poll_interval_seconds=10,
        )
        observation = ProductObservation(
            title="Sample product",
            price_ron=Decimal("100.00"),
            in_stock=True,
            can_purchase=True,
            page_url=config.product_url,
            observed_at=now,
        )
        monitor_events = []
        checked = monitor_until_ready(
            ReadyProductPage(observation),
            config,
            deadline=now + timedelta(minutes=2),
            clock=FixedClock(now),
            stop_event=threading.Event(),
            log=lambda *event: monitor_events.append(event),
        )
        self.assertIsNotNone(checked)

        cart = ReviewOnlyCart()
        _cart_snapshot, review = CheckoutPreparer(cart).prepare(config, checked)
        self.assertEqual(review.product_title, "Sample product")
        self.assertEqual(review.cart_total_ron, Decimal("112.00"))
        self.assertEqual(cart.calls, ["prepare_cart", "proceed_to_checkout"])
        for owner in (CheckoutPreparer, EmagPage):
            for forbidden_name in ("place_order", "authorize", "authorize_payment"):
                self.assertFalse(hasattr(owner, forbidden_name))
        self.assertTrue(hasattr(CheckoutPreparer, "submit_automatic_order"))

    def test_checkout_subtotal_change_stops_before_review_is_accepted(self):
        class ChangedCheckoutTotal(ReviewOnlyCart):
            def proceed_to_checkout(self, config, product):
                review = super().proceed_to_checkout(config, product)
                return CheckoutReview(
                    product_title=review.product_title,
                    quantity=review.quantity,
                    cart_total_ron=Decimal("117.00"),
                    payment_options=review.payment_options,
                    checkout_url=review.checkout_url,
                    product_subtotal_ron=Decimal("105.00"),
                    digital_delivery_confirmed=True,
                )

        with self.assertRaisesRegex(RuntimeError, "subtotal changed"):
            CheckoutPreparer(ChangedCheckoutTotal()).prepare(
                ProductConfig(
                    product_url="https://www.emag.ro/item/pd/AB12CD3BM/",
                    max_price_ron=Decimal("100.00"),
                    quantity=1,
                    target_date="2026-11-06",
                    target_time="12:00:00",
                    max_attempts=1,
                ),
                ProductObservation(
                    title="Sample product",
                    price_ron=Decimal("100.00"),
                    in_stock=True,
                    can_purchase=True,
                    page_url="https://www.emag.ro/item/pd/AB12CD3BM/",
                    observed_at=datetime.now().astimezone(),
                ),
            )

    def test_browser_code_has_only_cart_add_and_checkout_navigation_clicks(self):
        project = Path(__file__).resolve().parents[1]
        click_scopes = []
        forbidden_calls = []
        press_scopes = []

        class Calls(ast.NodeVisitor):
            def __init__(self):
                self.scope = []

            def visit_FunctionDef(self, node):
                self.scope.append(node.name)
                self.generic_visit(node)
                self.scope.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
                if name == "click":
                    click_scopes.append(self.scope[-1] if self.scope else "<module>")
                if name == "press":
                    press_scopes.append((self.scope[-1] if self.scope else "<module>", node.args))
                if name in {"place_order", "submit_order", "authorize", "authorize_payment"}:
                    forbidden_calls.append(name)
                self.generic_visit(node)

        for source in project.rglob("*.py"):
            if "tests" in source.parts or ".venv" in source.parts or "data" in source.parts:
                continue
            source_text = source.read_text(encoding="utf-8")
            Calls().visit(ast.parse(source_text, filename=str(source)))

        self.assertEqual(
            sorted(click_scopes),
            [
                "_confirm_digital_delivery_if_present",
                "_set_quantity",
                "prepare_cart",
                "prepare_multi_cart",
                "proceed_to_checkout",
                "proceed_to_checkout_multi",
                "submit_order_once",
            ],
        )
        self.assertEqual(forbidden_calls, [])
        self.assertTrue(all(scope == "_set_quantity" for scope, _args in press_scopes))

    def test_automatic_order_policy_allows_exactly_100_ron_and_blocks_over(self):
        purchase = PreparedPurchase(
            product_title="Sample digital ticket",
            unit_price_ron=Decimal("90.00"),
            quantity=1,
            cart_total_ron=Decimal("100.00"),
            payment_options=("Card online",),
            checkout_url="https://www.emag.ro/cart/checkout",
            is_digital_ticket=True,
            digital_delivery_confirmed=True,
        )
        CheckoutPreparer.validate_automatic_order(purchase)
        above = PreparedPurchase(
            **{**purchase.__dict__, "cart_total_ron": Decimal("100.01")}
        )
        with self.assertRaisesRegex(AutomaticOrderBlocked, "hard limit"):
            CheckoutPreparer.validate_automatic_order(above)

    def test_automatic_order_policy_rejects_nonpositive_total(self):
        purchase = PreparedPurchase(
            product_title="Sample digital ticket",
            unit_price_ron=Decimal("0.00"),
            quantity=1,
            cart_total_ron=Decimal("0.00"),
            payment_options=(),
            checkout_url="https://www.emag.ro/cart/checkout",
            is_digital_ticket=True,
            digital_delivery_confirmed=True,
        )
        with self.assertRaisesRegex(AutomaticOrderBlocked, "missing or invalid"):
            CheckoutPreparer.validate_automatic_order(purchase)

    def test_automatic_order_requires_confirmed_digital_recipient(self):
        purchase = PreparedPurchase(
            product_title="Sample physical item",
            unit_price_ron=Decimal("20.00"),
            quantity=1,
            cart_total_ron=Decimal("20.00"),
            payment_options=(),
            checkout_url="https://www.emag.ro/cart/checkout",
            digital_delivery_confirmed=False,
        )
        with self.assertRaisesRegex(AutomaticOrderBlocked, "explicitly marked digital ticket"):
            CheckoutPreparer.validate_automatic_order(purchase)


if __name__ == "__main__":
    unittest.main()
