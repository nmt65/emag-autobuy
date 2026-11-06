import unittest
from datetime import datetime
from decimal import Decimal

from core.conditions import (
    ConfigurationError,
    ProductConfig,
    ProductObservation,
    QuantityControlCandidate,
    choose_quantity_control,
    evaluate_product_conditions,
    quantity_within_limits,
    validate_emag_product_url,
    verify_cart,
)


def make_config(**overrides):
    values = {
        "product_url": "https://www.emag.ro/item/pd/AB12CD3BM/",
        "max_price_ron": Decimal("250.00"),
        "quantity": 2,
        "target_date": "2026-11-06",
        "target_time": "12:00:00",
        "max_attempts": 3,
    }
    values.update(overrides)
    return ProductConfig(**values)


class ConditionTests(unittest.TestCase):
    def test_rejects_non_emag_and_non_product_urls(self):
        with self.assertRaises(ConfigurationError):
            validate_emag_product_url("https://example.com/item")
        with self.assertRaises(ConfigurationError):
            validate_emag_product_url("https://www.emag.ro/")

    def test_price_and_stock_conditions(self):
        ready = ProductObservation(
            title="Sample product",
            price_ron=Decimal("249.99"),
            in_stock=True,
            can_purchase=True,
            page_url="https://www.emag.ro/item/pd/AB12CD3BM/",
            observed_at=datetime.now().astimezone(),
        )
        self.assertTrue(evaluate_product_conditions(ready, make_config()).ready)

        too_expensive = ProductObservation(
            **{**ready.__dict__, "price_ron": Decimal("250.01")}
        )
        self.assertFalse(evaluate_product_conditions(too_expensive, make_config()).ready)

        out_of_stock = ProductObservation(**{**ready.__dict__, "in_stock": False})
        self.assertFalse(evaluate_product_conditions(out_of_stock, make_config()).ready)
        unknown_stock = ProductObservation(**{**ready.__dict__, "in_stock": None})
        self.assertFalse(
            evaluate_product_conditions(
                unknown_stock, make_config(must_be_in_stock=False)
            ).ready
        )

    def test_five_ron_maximum_is_inclusive_and_rejects_one_ban_over(self):
        base = ProductObservation(
            title="Sample product",
            price_ron=Decimal("5.00"),
            in_stock=True,
            can_purchase=True,
            page_url="https://www.emag.ro/item/pd/AB12CD3BM/",
            observed_at=datetime.now().astimezone(),
        )
        self.assertTrue(
            evaluate_product_conditions(base, make_config(max_price_ron=Decimal("5.00"))).ready
        )
        above = ProductObservation(**{**base.__dict__, "price_ron": Decimal("5.01")})
        self.assertFalse(
            evaluate_product_conditions(above, make_config(max_price_ron=Decimal("5.00"))).ready
        )

    def test_configured_quantity_limits_and_visible_control_limits(self):
        make_config(quantity=1).validate()
        make_config(quantity=999).validate()
        for quantity in (0, 1000):
            with self.subTest(quantity=quantity), self.assertRaises(ConfigurationError):
                make_config(quantity=quantity).validate()
        self.assertTrue(quantity_within_limits(3, minimum=1, maximum=5))
        self.assertFalse(quantity_within_limits(6, minimum=1, maximum=5))
        self.assertFalse(quantity_within_limits(2, minimum=3, maximum=5))
        self.assertTrue(quantity_within_limits(2, options=("1", "2", "3")))
        self.assertFalse(quantity_within_limits(4, options=("1", "2", "3")))

    def test_quantity_control_must_be_unambiguous(self):
        one = [
            QuantityControlCandidate(
                control_type="number",
                name="quantity",
                value="1",
                row_text="Sample product 1 buc",
            )
        ]
        self.assertEqual(choose_quantity_control(one, "Sample product"), 0)
        many = [
            QuantityControlCandidate(
                control_type="number",
                name="quantity",
                value="1",
            ),
            QuantityControlCandidate(
                control_type="number",
                name="quantity",
                value="1",
            )
        ]
        self.assertIsNone(choose_quantity_control(many, "Sample product"))
        self.assertIsNone(
            choose_quantity_control(
                [QuantityControlCandidate(control_type="text", name="voucher")],
                "Sample product",
            )
        )

    def test_cart_verification_checks_product_quantity_price_and_total(self):
        good = verify_cart(
            visible_cart_text="Sample product 2 buc Total: 499,98 Lei",
            expected_title="Sample product",
            requested_quantity=2,
            observed_quantity=2,
            unit_price_ron=Decimal("249.99"),
            cart_total_ron=Decimal("499.98"),
            max_price_ron=Decimal("250.00"),
            expected_unit_price_ron=Decimal("249.99"),
            product_subtotal_ron=Decimal("499.98"),
        )
        self.assertTrue(good.verified)
        wrong_quantity = verify_cart(
            visible_cart_text="Sample product",
            expected_title="Sample product",
            requested_quantity=2,
            observed_quantity=1,
            unit_price_ron=Decimal("249.99"),
            cart_total_ron=Decimal("249.99"),
            max_price_ron=Decimal("250.00"),
            expected_unit_price_ron=Decimal("249.99"),
            product_subtotal_ron=Decimal("249.99"),
        )
        self.assertFalse(wrong_quantity.verified)


if __name__ == "__main__":
    unittest.main()
