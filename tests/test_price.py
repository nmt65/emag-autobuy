import unittest
from decimal import Decimal

from core.conditions import (
    detect_availability,
    parse_cart_total,
    parse_order_total_breakdown,
    parse_romanian_price,
)


class PriceParsingTests(unittest.TestCase):
    def test_price_uses_romanian_decimal_comma(self):
        self.assertEqual(parse_romanian_price("219,17 Lei"), Decimal("219.17"))

    def test_price_rejects_crossed_out_and_current_prices_in_same_text(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            parse_romanian_price("PRP: 1.000,99 Lei 729 , 99 Lei")

    def test_product_price_element_contains_only_the_displayed_current_price(self):
        self.assertEqual(parse_romanian_price("2.399,99 Lei"), Decimal("2399.99"))

    def test_cart_total_is_read_from_total_line(self):
        self.assertEqual(
            parse_cart_total(
                "Cost produse: 1.250,00 Lei\n"
                "Cost livrare: 20,00 Lei\n"
                "Taxă servicii: 5,00 Lei\n"
                "Total de plată: 1.275,00 Lei"
            ),
            Decimal("1275.00"),
        )

    def test_cart_total_reconciles_delivery_fees_and_discounts(self):
        breakdown = parse_order_total_breakdown(
            "Cost produse: 10,00 Lei\n"
            "Cost livrare: Gratuit\n"
            "Taxă manipulare: 2,00 Lei\n"
            "Discount voucher: -1,00 Lei\n"
            "Total: 11,00 Lei"
        )
        self.assertTrue(breakdown.complete, breakdown.reasons)
        self.assertEqual(breakdown.product_subtotal_ron, Decimal("10.00"))
        self.assertEqual(breakdown.total_ron, Decimal("11.00"))

    def test_checkout_total_includes_emag_operational_services_fee(self):
        breakdown = parse_order_total_breakdown(
            "Cost produse: 3,38 Lei\n"
            "Servicii operationale: 0,99 Lei\n"
            "Cost livrare: 12,99 Lei\n"
            "Total: 17,36 Lei"
        )
        self.assertTrue(breakdown.complete, breakdown.reasons)
        self.assertEqual(breakdown.total_ron, Decimal("17.36"))
        self.assertEqual(breakdown.product_subtotal_ron, Decimal("3.38"))

    def test_checkout_total_reconciles_amounts_split_across_visible_text_nodes(self):
        breakdown = parse_order_total_breakdown(
            "Sumar comandă\n"
            "Cost produse:\n3\n,\n38\nLei\n"
            "Servicii operationale: \n0\n,\n99\nLei\n"
            "Cost livrare:\n12\n,\n99\nLei\n"
            "Total:\n17\n,\n36\nLei\n"
            "Prin plasarea comenzii, ești de acord cu termenii."
        )
        self.assertTrue(breakdown.complete, breakdown.reasons)
        self.assertEqual(breakdown.product_subtotal_ron, Decimal("3.38"))
        self.assertEqual(breakdown.total_ron, Decimal("17.36"))

    def test_cart_total_fails_closed_on_missing_delivery_or_unreconciled_fee(self):
        self.assertIsNone(
            parse_cart_total("Cost produse: 10,00 Lei\nTotal: 10,00 Lei")
        )
        self.assertIsNone(
            parse_cart_total(
                "Cost produse: 10,00 Lei\n"
                "Cost livrare: Gratuit\n"
                "Taxă manipulare: 2,00 Lei\n"
                "Total: 10,00 Lei"
            )
        )

    def test_missing_price_and_total_are_unknown(self):
        with self.assertRaises(ValueError):
            parse_romanian_price("Pret indisponibil")
        self.assertIsNone(parse_cart_total("Cost produse: 0,00 Lei"))


class AvailabilityTests(unittest.TestCase):
    def test_detects_stock_states(self):
        self.assertTrue(detect_availability("\u00cen stoc"))
        self.assertFalse(detect_availability("Nu este disponibil"))
        self.assertFalse(detect_availability("Momentan indisponibil"))
        self.assertIsNone(detect_availability("Livrare estimată"))


if __name__ == "__main__":
    unittest.main()
