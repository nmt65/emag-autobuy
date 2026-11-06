from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from browser.emag import CartSnapshot, CheckoutReview, EmagPage
from core.conditions import ProductConfig, ProductObservation


MAX_AUTOMATIC_ORDER_TOTAL_RON = Decimal("100.00")


class AutomaticOrderBlocked(RuntimeError):
    """The order cannot be submitted under the fixed user-approved policy."""


@dataclass(frozen=True)
class OrderSubmissionResult:
    status: str
    message: str


@dataclass(frozen=True)
class PreparedPurchaseItem:
    product_title: str
    unit_price_ron: Decimal
    quantity: int


@dataclass(frozen=True)
class PreparedPurchase:
    product_title: str
    unit_price_ron: Decimal
    quantity: int
    cart_total_ron: Decimal
    payment_options: tuple[str, ...]
    checkout_url: str
    product_subtotal_ron: Decimal | None = None
    is_digital_ticket: bool = False
    digital_delivery_confirmed: bool = False
    items: tuple[PreparedPurchaseItem, ...] = ()


class CheckoutPreparer:
    """Prepare checkout and submit only after the strict total and identity checks."""

    def __init__(self, emag: EmagPage) -> None:
        self.emag = emag

    def prepare(
        self, config: ProductConfig, product: ProductObservation
    ) -> tuple[CartSnapshot, PreparedPurchase]:
        cart = self.emag.prepare_cart(config, product)
        review: CheckoutReview = self.emag.proceed_to_checkout(config, product)
        if cart.cart_unit_price_ron is None or cart.cart_total_ron is None:
            raise RuntimeError("The cart price or total was not verified.")
        if (
            cart.product_subtotal_ron is None
            or review.product_subtotal_ron is None
            or review.product_subtotal_ron != cart.product_subtotal_ron
        ):
            raise RuntimeError(
                "The checkout product subtotal changed or could not be reconciled with the cart."
            )
        if review.cart_total_ron <= 0:
            raise RuntimeError("The checkout total was not visible and could not be verified.")
        prepared = PreparedPurchase(
            product_title=review.product_title,
            unit_price_ron=cart.cart_unit_price_ron,
            quantity=review.quantity,
            cart_total_ron=review.cart_total_ron,
            payment_options=review.payment_options,
            checkout_url=review.checkout_url,
            product_subtotal_ron=review.product_subtotal_ron,
            is_digital_ticket=config.is_digital_ticket,
            digital_delivery_confirmed=review.digital_delivery_confirmed,
        )
        return cart, prepared

    def prepare_multi(
        self,
        configs: tuple[ProductConfig, ...],
        products: tuple[ProductObservation, ...],
    ) -> tuple[tuple[CartSnapshot, ...], PreparedPurchase]:
        if len(configs) < 2 or len(configs) != len(products):
            raise RuntimeError("Shared-cart checkout requires at least two complete products.")
        if not hasattr(self.emag, "prepare_multi_cart") or not hasattr(
            self.emag, "proceed_to_checkout_multi"
        ):
            raise RuntimeError("This browser adapter does not support a verified shared cart.")
        carts = self.emag.prepare_multi_cart(configs, products)
        if len(carts) != len(configs):
            raise RuntimeError("Not every configured item has a verified cart row.")
        review: CheckoutReview = self.emag.proceed_to_checkout_multi(configs, products)
        items: list[PreparedPurchaseItem] = []
        for config, product, cart in zip(configs, products, carts, strict=True):
            if (
                not cart.product_present
                or cart.quantity != config.quantity
                or cart.cart_unit_price_ron is None
                or cart.cart_unit_price_ron != product.price_ron
                or cart.cart_unit_price_ron > config.max_price_ron
            ):
                raise RuntimeError(
                    f"The shared cart row for {product.title!r} failed final verification."
                )
            items.append(
                PreparedPurchaseItem(
                    product_title=product.title,
                    unit_price_ron=cart.cart_unit_price_ron,
                    quantity=config.quantity,
                )
            )
        expected_subtotal = sum(
            (item.unit_price_ron * item.quantity for item in items), Decimal("0.00")
        )
        cart_subtotal = carts[0].product_subtotal_ron
        cart_total = carts[0].cart_total_ron
        if any(
            cart.product_subtotal_ron != cart_subtotal
            or cart.cart_total_ron != cart_total
            for cart in carts
        ):
            raise RuntimeError(
                "The shared-cart snapshots do not agree on their subtotal and total."
            )
        if cart_subtotal != expected_subtotal or review.product_subtotal_ron != expected_subtotal:
            raise RuntimeError(
                "The shared-cart subtotal does not reconcile across cart rows and checkout."
            )
        if cart_total is None or review.cart_total_ron <= 0:
            raise RuntimeError("The shared-cart total was not verified.")
        prepared = PreparedPurchase(
            product_title=review.product_title,
            unit_price_ron=expected_subtotal,
            quantity=sum(item.quantity for item in items),
            cart_total_ron=review.cart_total_ron,
            payment_options=review.payment_options,
            checkout_url=review.checkout_url,
            product_subtotal_ron=expected_subtotal,
            is_digital_ticket=all(config.is_digital_ticket for config in configs),
            digital_delivery_confirmed=review.digital_delivery_confirmed,
            items=tuple(items),
        )
        return carts, prepared

    @staticmethod
    def validate_automatic_order(prepared: PreparedPurchase) -> None:
        if not prepared.is_digital_ticket or not prepared.digital_delivery_confirmed:
            raise AutomaticOrderBlocked(
                "Automatic ordering requires an explicitly marked digital ticket and a confirmed recipient."
            )
        if prepared.cart_total_ron <= 0:
            raise AutomaticOrderBlocked("The payable total is missing or invalid.")
        if prepared.cart_total_ron > MAX_AUTOMATIC_ORDER_TOTAL_RON:
            raise AutomaticOrderBlocked(
                f"Checkout total {prepared.cart_total_ron:.2f} RON exceeds the hard "
                f"limit of {MAX_AUTOMATIC_ORDER_TOTAL_RON:.2f} RON. No order was submitted."
            )

    def submit_automatic_order(
        self,
        prepared: PreparedPurchase,
        *,
        stop_event=None,
        expected_product_titles: tuple[str, ...] | None = None,
    ) -> OrderSubmissionResult:
        self.validate_automatic_order(prepared)
        if not hasattr(self.emag, "validate_order_submission") or not hasattr(
            self.emag, "submit_order_once"
        ):
            raise AutomaticOrderBlocked(
                "This checkout adapter does not support the verified automatic order step."
            )
        titles = expected_product_titles or tuple(
            item.product_title for item in prepared.items
        ) or (prepared.product_title,)
        self.emag.validate_order_submission(
            prepared.cart_total_ron,
            expected_product_subtotal_ron=prepared.product_subtotal_ron,
            expected_product_titles=titles,
        )
        result = self.emag.submit_order_once(
            prepared.cart_total_ron,
            stop_event=stop_event,
            expected_product_subtotal_ron=prepared.product_subtotal_ron,
            expected_product_titles=titles,
        )
        return OrderSubmissionResult(status=result.status, message=result.message)
