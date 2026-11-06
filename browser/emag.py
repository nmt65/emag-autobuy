from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
except ImportError:  # Keep offline unit tests for parsing and conditions dependency-light.
    from typing import Any

    class PlaywrightError(Exception):
        """Fallback type used only when Playwright is not installed."""

    class PlaywrightTimeoutError(PlaywrightError):
        """Fallback timeout type used only when Playwright is not installed."""

    Page = Any

from core.conditions import (
    ProductConfig,
    ProductObservation,
    QuantityControlCandidate,
    choose_quantity_control,
    detect_availability,
    is_quantity_control_candidate,
    parse_order_total_breakdown,
    parse_romanian_price,
    quantity_within_limits,
    validate_emag_product_url,
    verify_cart,
)


HOME_URL = "https://www.emag.ro/"
CART_URL = "https://www.emag.ro/cart/products?ref=cart"
CHECKOUT_URL = "https://www.emag.ro/cart/checkout"
PRODUCT_TITLE_SELECTOR = "h1.page-title"
PRODUCT_PRICE_SELECTOR = '[data-test="main-price"]'
PRODUCT_STOCK_SELECTOR = ".label-in_stock"
PRODUCT_ADD_BUTTON_SELECTOR = 'button[data-test="main-add-to-cart-button"]'


class EmagWorkflowError(RuntimeError):
    """Expected, user-actionable failure while reading or preparing a cart."""


class AuthenticationRequired(EmagWorkflowError):
    """The user must finish eMAG sign-in manually in the visible browser."""


class CaptchaRequired(EmagWorkflowError):
    """A site challenge was shown; the app stops without solving or bypassing it."""


class PageStructureChanged(EmagWorkflowError):
    """The visible page no longer contains the inspected controls."""


class CartActionError(EmagWorkflowError):
    """The cart could not be prepared and verified safely."""


class CheckoutFlowBlocked(EmagWorkflowError):
    """The checkout or digital delivery step is not uniquely understood."""


@dataclass(frozen=True)
class QuantityField:
    element_index: int
    control_type: str
    value: str
    options: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class CartSnapshot:
    visible_text: str
    product_present: bool
    cart_unit_price_ron: Decimal | None
    quantity_field: QuantityField | None
    quantity: int | None
    cart_total_ron: Decimal | None
    product_subtotal_ron: Decimal | None
    cart_known_empty: bool
    ambiguous_product_reference: bool
    quantity_control_count: int


@dataclass(frozen=True)
class CheckoutReview:
    product_title: str
    quantity: int
    cart_total_ron: Decimal
    payment_options: tuple[str, ...]
    checkout_url: str
    product_subtotal_ron: Decimal | None = None
    digital_delivery_confirmed: bool = False


@dataclass(frozen=True)
class EmagOrderResult:
    status: str
    message: str


def _product_code(url: str) -> str | None:
    match = re.search(r"/pd/([^/?#]+)", url, re.IGNORECASE)
    return match.group(1).upper() if match else None


def _safe_payment_method_label(label: str) -> str | None:
    normalized = " ".join((label or "").split())
    if not normalized or re.search(r"\d|[•●*]{2,}", normalized):
        return None
    return normalized


def _normalized_ui_text(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value or "").casefold()
    return "".join(char for char in folded if not unicodedata.combining(char))


def _visible_order_summary(root) -> dict:
    """Read one uniquely marked visible summary and its complete charge block."""
    return root.evaluate(
        """root => {
          const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const normalize = value => (value || '').normalize('NFD')
            .replace(/[\\u0300-\\u036f]/g, '').toLowerCase().replace(/\\s+/g, ' ').trim();
          const markers = [...root.querySelectorAll('*')]
            .filter(el => visible(el) && normalize(el.innerText) === 'sumar comanda'
              && ![...el.children].some(child => normalize(child.innerText) === 'sumar comanda'));
          if (markers.length !== 1) return {markerCount: markers.length, text: ''};
          let node = markers[0];
          while (node && node !== root) {
            const text = (node.innerText || '').trim();
            if (/cost produse|subtotal produse/i.test(text)
                && /cost livrare|taxa livrare|transport|shipping/i.test(normalize(text))
                && /\\btotal(?: de plata| de achitat| comanda)?\\s*:/i.test(normalize(text))) {
              return {markerCount: 1, text};
            }
            node = node.parentElement;
          }
          return {markerCount: 1, text: ''};
        }"""
    )


class EmagPage:
    """Adapter for eMAG's visible product, cart, and checkout pages."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self._expected_url: str | None = None
        self._expected_code: str | None = None

    def _raise_if_login_or_challenge(self) -> None:
        host = (urlparse(self.page.url).hostname or "").lower()
        if host == "auth.emag.ro" or "/user/login" in self.page.url:
            raise AuthenticationRequired(
                "eMAG needs sign-in. Complete it manually in the browser, then resume."
            )
        try:
            text = self.page.locator("body").inner_text(timeout=2_000).casefold()
        except PlaywrightError:
            text = ""
        if "captcha" in text or "verify you are human" in text or "confirmă că ești om" in text:
            raise CaptchaRequired(
                "eMAG displayed a verification challenge. Complete it manually; the app will not bypass it."
            )

    def open_product(self, product_url: str) -> None:
        safe_url = validate_emag_product_url(product_url)
        self._expected_url = safe_url
        self._expected_code = _product_code(safe_url)
        self.page.goto(safe_url, wait_until="domcontentloaded")
        self._raise_if_login_or_challenge()
        try:
            self.page.locator(PRODUCT_TITLE_SELECTOR).wait_for(state="visible")
        except PlaywrightTimeoutError as exc:
            raise PageStructureChanged(
                "Could not find the visible product title. eMAG may have changed its page."
            ) from exc
        self._verify_product_identity()

    def _verify_product_identity(self) -> str:
        self._raise_if_login_or_challenge()
        title_matches = self._visible_matches(PRODUCT_TITLE_SELECTOR)
        if len(title_matches) != 1:
            raise PageStructureChanged(
                "The visible product title is missing or ambiguous; stopping."
            )
        title = title_matches[0].inner_text().strip()
        if not title:
            raise PageStructureChanged("The eMAG product title is empty.")
        actual_code = _product_code(self.page.url)
        if self._expected_code and actual_code != self._expected_code:
            raise PageStructureChanged(
                "The product page redirected to a different product or variant; stopping."
            )
        return title

    def _visible_matches(self, selector: str) -> list:
        locator = self.page.locator(selector)
        visible = []
        for index in range(locator.count()):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible():
                    visible.append(candidate)
            except PlaywrightError:
                continue
        return visible

    def refresh_product(self) -> None:
        """Refresh the visible product page once per configured polling interval."""
        self.page.reload(wait_until="domcontentloaded")
        self._raise_if_login_or_challenge()
        self._verify_product_identity()

    def read_product(self, product_url: str | None = None) -> ProductObservation:
        if product_url is not None and (
            self._expected_url != validate_emag_product_url(product_url)
            or self.page.url.rstrip("/") != product_url.rstrip("/")
        ):
            self.open_product(product_url)
        title = self._verify_product_identity()

        price: Decimal | None = None
        price_matches = self._visible_matches(PRODUCT_PRICE_SELECTOR)
        try:
            if len(price_matches) == 1:
                price = parse_romanian_price(price_matches[0].inner_text())
        except (PlaywrightError, ValueError):
            price = None

        stock_text = ""
        stock_matches = self._visible_matches(PRODUCT_STOCK_SELECTOR)
        try:
            if len(stock_matches) == 1:
                stock_text = stock_matches[0].inner_text()
        except PlaywrightError:
            stock_text = ""
        availability = detect_availability(stock_text)

        add_buttons = self._visible_matches(PRODUCT_ADD_BUTTON_SELECTOR)
        try:
            can_purchase = (
                len(add_buttons) == 1 and add_buttons[0].is_enabled()
            )
        except PlaywrightError:
            can_purchase = False

        return ProductObservation(
            title=title,
            price_ron=price,
            in_stock=availability,
            can_purchase=can_purchase,
            page_url=self.page.url,
            observed_at=datetime.now(timezone.utc),
        )

    def _cart_snapshot(self, product_title: str) -> CartSnapshot:
        self._raise_if_login_or_challenge()
        main = self.page.locator("main")
        try:
            main.wait_for(state="visible")
            text = main.inner_text()
        except PlaywrightTimeoutError as exc:
            raise CartActionError("The cart page did not load its visible contents.") from exc

        normalized_text = " ".join(text.casefold().split())
        normalized_title = " ".join(product_title.casefold().split())
        summary_info = _visible_order_summary(main)

        raw_controls = self.page.locator("main input, main select")
        candidates: list[QuantityControlCandidate] = []
        element_refs: list[tuple[str, int]] = []
        metadata: list[dict] = []
        for index in range(raw_controls.count()):
            locator = raw_controls.nth(index)
            try:
                data = locator.evaluate(
                    """(el, title) => {
                      const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                      const label = el.labels ? [...el.labels].map(x => x.innerText).join(' ') : '';
                      const visibleNode = node => !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
                      const isQuantityControl = node => {
                        const metadata = [node.getAttribute('name'), node.getAttribute('aria-label'), node.getAttribute('title'),
                          node.labels ? [...node.labels].map(item => item.innerText).join(' ') : ''].join(' ');
                        return visibleNode(node) && (/^(number|select)$/.test(node.tagName.toLowerCase() === 'select' ? 'select' : (node.getAttribute('type') || '').toLowerCase())
                          || /cantit|quantity|qty|buc[aă]ți/i.test(metadata));
                      };
                      let row = el.parentElement;
                      let rowText = '';
                      for (let depth = 0; row && depth < 7; depth++, row = row.parentElement) {
                        const text = (row.innerText || '').trim();
                        if (text && text.length < 1200 && text.toLowerCase().includes(title.toLowerCase())) {
                          const controls = [row, ...row.querySelectorAll('input,select')].filter(isQuantityControl);
                          if (controls.length === 1) {
                            rowText = text;
                            break;
                          }
                        }
                      }
                      return {
                        visible, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
                        name: el.getAttribute('name') || '', aria: el.getAttribute('aria-label') || '',
                        title: el.getAttribute('title') || '', label, value: el.value || '',
                        rowText, options: el.tagName === 'SELECT' ? [...el.options].map(o => o.value) : [],
                        minimum: el.min !== '' && Number.isInteger(Number(el.min)) ? Number(el.min) : null,
                        maximum: el.max !== '' && Number.isInteger(Number(el.max)) ? Number(el.max) : null
                      };
                    }""",
                    product_title,
                )
                if not data["visible"]:
                    continue
                control_type = data["type"] or data["tag"]
                candidates.append(
                    QuantityControlCandidate(
                        control_type=control_type,
                        name=" ".join((data["name"], data["aria"], data["title"])),
                        label=data["label"],
                        value=data["value"],
                        row_text=data["rowText"],
                        visible=True,
                        minimum=data["minimum"],
                        maximum=data["maximum"],
                        options=tuple(str(value) for value in data["options"]),
                    )
                )
                element_refs.append(("field", index))
                metadata.append(data)
            except PlaywrightError:
                continue

        # The inspected live cart exposes quantity as an accessible group with
        # decrement/increment buttons and a displayed value, rather than an input.
        quantity_groups = main.get_by_role(
            "group", name=re.compile(r"^cantitate$", re.IGNORECASE)
        )
        for index in range(quantity_groups.count()):
            group = quantity_groups.nth(index)
            try:
                data = group.evaluate(
                    """(el, title) => {
                      const groupVisible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                      let row = el.parentElement;
                      let rowText = '';
                      for (let depth = 0; row && depth < 7; depth++, row = row.parentElement) {
                        const text = (row.innerText || '').trim();
                        if (text && text.length < 1200) {
                          const quantityGroups = [...row.querySelectorAll('[role="group"]')]
                            .filter(item => {
                              const visible = item.offsetWidth || item.offsetHeight || item.getClientRects().length;
                              return visible && /^(cantitate|quantity)$/i.test(item.getAttribute('aria-label') || item.querySelector('legend')?.innerText || '');
                            });
                          if (text.toLowerCase().includes(title.toLowerCase()) && quantityGroups.length === 1) {
                            rowText = text;
                            break;
                          }
                        }
                      }
                      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                      const values = [];
                      let node;
                      while ((node = walker.nextNode())) {
                        const value = (node.nodeValue || '').trim();
                        const parent = node.parentElement;
                        if (/^\\d{1,3}$/.test(value) && parent && (parent.offsetWidth || parent.offsetHeight || parent.getClientRects().length)
                            && !parent.closest('button,[aria-hidden="true"]')) values.push(value);
                      }
                      return {
                        visible: groupVisible,
                        name: el.getAttribute('aria-label') || el.querySelector('legend')?.innerText || 'Cantitate',
                        label: el.getAttribute('aria-label') || el.querySelector('legend')?.innerText || 'Cantitate',
                        value: values.length === 1 ? values[0] : '',
                        valueCount: values.length, rowText
                      };
                    }""",
                    product_title,
                )
                if not data["visible"]:
                    continue
                candidates.append(
                    QuantityControlCandidate(
                        control_type="button-group",
                        name=str(data["name"]),
                        label=str(data["label"]),
                        value=str(data["value"]),
                        row_text=str(data["rowText"]),
                        visible=True,
                        minimum=1,
                        maximum=None,
                    )
                )
                element_refs.append(("group", index))
                metadata.append(data)
            except PlaywrightError:
                continue

        selected = choose_quantity_control(candidates, product_title)
        field: QuantityField | None = None
        quantity: int | None = None
        product_row_text = ""
        if selected is not None:
            data = metadata[selected]
            product_row_text = str(data["rowText"])
            try:
                quantity = int(data["value"])
            except (TypeError, ValueError):
                quantity = None
            ref_kind, ref_index = element_refs[selected]
            field = QuantityField(
                element_index=ref_index,
                control_type=(
                    "button-group"
                    if ref_kind == "group"
                    else data["tag"] if data["tag"] == "select" else data["type"]
                ),
                value=str(data["value"]),
                options=tuple(str(value) for value in data.get("options", [])),
                minimum=data.get("minimum"),
                maximum=data.get("maximum"),
            )
        product_present = selected is not None
        likely_control_count = sum(
            1 for candidate in candidates if is_quantity_control_candidate(candidate)
        )
        cart_heading = main.get_by_role(
            "heading", name=re.compile(r"^coșul meu$", re.IGNORECASE)
        )
        recommendation_heading = main.get_by_text(
            re.compile(r"^produse alese pentru tine$", re.IGNORECASE)
        )
        remove_buttons = main.get_by_role(
            "button", name=re.compile(r"^șterge$|^sterge$", re.IGNORECASE)
        )
        cart_known_empty = bool(
            cart_heading.count() == 1
            and cart_heading.is_visible()
            and recommendation_heading.count() == 1
            and recommendation_heading.is_visible()
            and likely_control_count == 0
            and remove_buttons.count() == 0
        )
        ambiguous_product_reference = bool(
            normalized_title
            and normalized_title in normalized_text
            and not product_present
            and not cart_known_empty
        )
        try:
            cart_unit_price = (
                parse_romanian_price(product_row_text) if product_row_text.strip() else None
            )
        except ValueError:
            cart_unit_price = None

        summary_text = str(summary_info.get("text", ""))
        breakdown = parse_order_total_breakdown(summary_text)
        return CartSnapshot(
            visible_text=text,
            product_present=product_present,
            cart_unit_price_ron=cart_unit_price,
            quantity_field=field,
            quantity=quantity,
            cart_total_ron=breakdown.total_ron if breakdown.complete else None,
            product_subtotal_ron=(
                breakdown.product_subtotal_ron if breakdown.complete else None
            ),
            cart_known_empty=cart_known_empty,
            ambiguous_product_reference=ambiguous_product_reference,
            quantity_control_count=likely_control_count,
        )

    def inspect_cart(self, product_title: str) -> CartSnapshot:
        self.page.goto(CART_URL, wait_until="domcontentloaded")
        self._raise_if_login_or_challenge()
        return self._cart_snapshot(product_title)

    def _verified_multi_cart_rows(
        self,
        configs: tuple[ProductConfig, ...],
        products: tuple[ProductObservation, ...],
        *,
        require_requested_quantities: bool,
    ) -> tuple[CartSnapshot, ...]:
        if not configs or len(configs) != len(products):
            raise CartActionError("The multi-product cart request is incomplete.")
        snapshots = tuple(self._cart_snapshot(product.title) for product in products)
        if any(snapshot.quantity_control_count != len(configs) for snapshot in snapshots):
            raise CartActionError(
                "The cart does not contain exactly one visible quantity row per configured product."
            )
        observed_subtotal = Decimal("0.00")
        for config, product, snapshot in zip(configs, products, snapshots, strict=True):
            if (
                not snapshot.product_present
                or snapshot.ambiguous_product_reference
                or snapshot.quantity_field is None
                or snapshot.quantity is None
            ):
                raise CartActionError(
                    f"The cart row for {product.title!r} is missing or ambiguous."
                )
            if snapshot.cart_unit_price_ron is None:
                raise CartActionError(
                    f"The cart unit price for {product.title!r} could not be verified."
                )
            if (
                snapshot.cart_unit_price_ron != product.price_ron
                or snapshot.cart_unit_price_ron > config.max_price_ron
            ):
                raise CartActionError(
                    f"The cart price for {product.title!r} changed or exceeds its configured maximum."
                )
            if require_requested_quantities and snapshot.quantity != config.quantity:
                raise CartActionError(
                    f"The cart quantity for {product.title!r} does not match the request."
                )
            if not quantity_within_limits(
                config.quantity,
                minimum=snapshot.quantity_field.minimum,
                maximum=snapshot.quantity_field.maximum,
                options=snapshot.quantity_field.options,
            ):
                raise CartActionError(
                    f"The requested quantity for {product.title!r} is outside the visible cart limits."
                )
            observed_subtotal += snapshot.cart_unit_price_ron * snapshot.quantity

        reference = snapshots[0]
        if reference.product_subtotal_ron != observed_subtotal:
            raise CartActionError(
                "The cart product subtotal does not equal the sum of its verified product rows "
                f"({reference.product_subtotal_ron!r} vs {observed_subtotal:.2f} RON)."
            )
        if reference.cart_total_ron is None:
            raise CartActionError(
                "The cart total and fee breakdown could not be verified."
            )
        return snapshots

    def prepare_multi_cart(
        self,
        configs: tuple[ProductConfig, ...],
        products: tuple[ProductObservation, ...],
    ) -> tuple[CartSnapshot, ...]:
        """Add configured products to one initially empty shared cart and verify every row."""
        if len(configs) < 2 or len(configs) != len(products):
            raise CartActionError("Shared-cart preparation requires at least two complete products.")
        initial = self.inspect_cart(products[0].title)
        if not initial.cart_known_empty:
            raise CartActionError(
                "The shared cart is not confirmed empty; no products were added."
            )

        added_configs: list[ProductConfig] = []
        added_products: list[ProductObservation] = []
        for config, monitored_product in zip(configs, products, strict=True):
            self.open_product(config.product_url)
            current = self.read_product()
            if (
                " ".join(current.title.casefold().split())
                != " ".join(monitored_product.title.casefold().split())
                or current.price_ron is None
                or current.price_ron > config.max_price_ron
                or (config.must_be_in_stock and current.in_stock is not True)
                or not current.can_purchase
            ):
                raise CartActionError(
                    f"Product conditions changed for {monitored_product.title!r}; it was not added."
                )
            add_buttons = self._visible_matches(PRODUCT_ADD_BUTTON_SELECTOR)
            if len(add_buttons) != 1 or not add_buttons[0].is_enabled():
                raise CartActionError(
                    f"The add-to-cart control for {current.title!r} is missing or ambiguous."
                )
            add_buttons[0].click()
            self._raise_if_login_or_challenge()
            self.page.goto(CART_URL, wait_until="domcontentloaded")
            added_configs.append(config)
            added_products.append(current)
            prefix_configs = tuple(added_configs)
            prefix_products = tuple(added_products)
            snapshots = self._verified_multi_cart_rows(
                prefix_configs,
                prefix_products,
                require_requested_quantities=False,
            )
            target_snapshot = snapshots[-1]
            if target_snapshot.quantity != config.quantity:
                self._set_and_verify_quantity(target_snapshot, current.title, config.quantity)

        final = self._verified_multi_cart_rows(
            tuple(added_configs),
            tuple(added_products),
            require_requested_quantities=True,
        )
        return final

    @staticmethod
    def _read_quantity_group(group) -> int:
        value = group.evaluate(
            """el => {
              const visible = node => !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
              const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
              const values = [];
              let node;
              while ((node = walker.nextNode())) {
                const text = (node.nodeValue || '').trim();
                const parent = node.parentElement;
                if (/^\\d{1,3}$/.test(text) && parent && visible(parent)
                    && !parent.closest('button,[aria-hidden="true"]')) values.push(Number(text));
              }
              return values.length === 1 ? values[0] : null;
            }"""
        )
        if type(value) is not int or value < 1:
            raise CartActionError(
                "The cart's visible quantity value is missing or ambiguous."
            )
        return value

    def _set_quantity(self, field: QuantityField, quantity: int) -> None:
        if field.control_type == "button-group":
            if abs(quantity - int(field.value)) > 10:
                raise CartActionError(
                    "The requested quantity would require more than 10 cart stepper actions; "
                    "stopping for manual review."
                )
            groups = self.page.locator("main").get_by_role(
                "group", name=re.compile(r"^cantitate$", re.IGNORECASE)
            )
            if field.element_index >= groups.count():
                raise CartActionError("The cart quantity group changed; stopping.")
            group = groups.nth(field.element_index)
            if not group.is_visible():
                raise CartActionError("The cart quantity group is no longer visible.")
            current = self._read_quantity_group(group)
            if current != int(field.value):
                raise CartActionError("The cart quantity changed during verification; stopping.")
            while current != quantity:
                increase = quantity > current
                label = "Crește cantitatea" if increase else "Scade cantitatea"
                button = group.get_by_role(
                    "button", name=re.compile(rf"^{re.escape(label)}$", re.IGNORECASE)
                )
                if button.count() != 1 or not button.is_visible() or not button.is_enabled():
                    raise CartActionError(
                        "The required cart quantity stepper is unavailable or ambiguous."
                    )
                button.click()
                previous = current
                for _ in range(20):
                    try:
                        current = self._read_quantity_group(group)
                    except PlaywrightError:
                        current = previous
                    if current != previous:
                        break
                    self.page.wait_for_timeout(250)
                if current == previous:
                    raise CartActionError(
                        "The cart did not confirm the requested quantity change."
                    )
            return

        locator = self.page.locator("main input, main select").nth(field.element_index)
        if field.control_type == "select":
            if str(quantity) not in field.options:
                raise CartActionError(
                    f"The cart does not offer quantity {quantity} for this product."
                )
            locator.select_option(str(quantity))
        elif field.control_type in {"number", "text", "tel"}:
            locator.fill(str(quantity))
            locator.press("Tab")
        else:
            raise CartActionError(
                "The cart quantity control is not a supported visible input."
            )

    def _set_and_verify_quantity(
        self, snapshot: CartSnapshot, product_title: str, quantity: int
    ) -> CartSnapshot:
        if not quantity_within_limits(
            quantity,
            minimum=snapshot.quantity_field.minimum if snapshot.quantity_field else None,
            maximum=snapshot.quantity_field.maximum if snapshot.quantity_field else None,
            options=snapshot.quantity_field.options if snapshot.quantity_field else (),
        ):
            raise CartActionError(
                "The requested quantity is outside the limits shown by the cart control."
            )
        if snapshot.quantity_field is None:
            raise CartActionError(
                "No unambiguous visible quantity control was found in the cart. "
                "The app stopped before changing an item."
            )
        if snapshot.quantity != quantity:
            self._set_quantity(snapshot.quantity_field, quantity)
            try:
                self.page.wait_for_load_state("networkidle", timeout=2_000)
            except PlaywrightTimeoutError:
                pass
            self.page.reload(wait_until="domcontentloaded")
        verified = self._cart_snapshot(product_title)
        if verified.quantity != quantity:
            raise CartActionError(
                f"The cart did not retain quantity {quantity}; observed {verified.quantity!r}."
            )
        return verified

    def prepare_cart(
        self, config: ProductConfig, product: ProductObservation
    ) -> CartSnapshot:
        # Re-read the displayed product price and stock immediately before cart work.
        self.open_product(config.product_url)
        product = self.read_product()
        if (
            product.price_ron is None
            or product.price_ron > config.max_price_ron
            or (config.must_be_in_stock and product.in_stock is not True)
            or not product.can_purchase
        ):
            raise CartActionError(
                "Product conditions changed before cart preparation; no cart action was taken."
            )
        # Read first so a retry never creates a duplicate cart line.
        snapshot = self.inspect_cart(product.title)
        if snapshot.ambiguous_product_reference:
            raise CartActionError(
                "The product title appears in the cart page without a matching quantity row; "
                "it may be a recommendation, so the app will not add it."
            )
        if not snapshot.product_present:
            if not snapshot.cart_known_empty:
                raise CartActionError(
                    "The cart is not confirmed empty and the target product is not verified; "
                    "the app will not add another item."
                )
            self.open_product(config.product_url)
            current = self.read_product()
            if (
                current.price_ron is None
                or current.price_ron > config.max_price_ron
                or (config.must_be_in_stock and current.in_stock is not True)
                or not current.can_purchase
            ):
                raise CartActionError(
                    "Product conditions changed before it could be added to the cart."
                )
            add_buttons = self._visible_matches(PRODUCT_ADD_BUTTON_SELECTOR)
            if len(add_buttons) != 1 or not add_buttons[0].is_enabled():
                raise CartActionError(
                    "The visible add-to-cart control is missing or ambiguous."
                )
            add_buttons[0].click()
            self._raise_if_login_or_challenge()
            self.page.goto(CART_URL, wait_until="domcontentloaded")
            snapshot = self._cart_snapshot(product.title)
        if not snapshot.product_present:
            raise CartActionError("eMAG did not show the product in the cart after adding it.")
        if snapshot.ambiguous_product_reference:
            raise CartActionError(
                "The product is visible in recommendations but not in a verified cart row."
            )
        if snapshot.quantity_control_count != 1:
            raise CartActionError(
                "The cart does not expose exactly one quantity control for this workflow."
            )

        snapshot = self._set_and_verify_quantity(snapshot, product.title, config.quantity)
        if snapshot.cart_total_ron is None:
            raise CartActionError("The visible cart total could not be verified.")
        if snapshot.cart_unit_price_ron is None:
            raise CartActionError("The cart's displayed unit price could not be verified.")
        result = verify_cart(
            visible_cart_text=snapshot.visible_text,
            expected_title=product.title,
            requested_quantity=config.quantity,
            observed_quantity=snapshot.quantity,
            unit_price_ron=snapshot.cart_unit_price_ron,
            cart_total_ron=snapshot.cart_total_ron,
            max_price_ron=config.max_price_ron,
            expected_unit_price_ron=product.price_ron,
            product_subtotal_ron=snapshot.product_subtotal_ron,
        )
        if not result.verified:
            raise CartActionError("Cart verification failed: " + " ".join(result.reasons))
        return snapshot

    def _confirm_digital_delivery_if_present(self) -> bool:
        """Confirm a prefilled digital-ticket recipient without reading it into logs."""
        main = self.page.locator("main")
        email_inputs = main.locator("input[type='email']")
        visible_email_inputs = []
        for index in range(email_inputs.count()):
            candidate = email_inputs.nth(index)
            if candidate.is_visible():
                visible_email_inputs.append(candidate)
        if len(visible_email_inputs) > 1:
            raise CheckoutFlowBlocked(
                "Several visible email fields were found; digital delivery is ambiguous."
            )

        email_pattern = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
        if visible_email_inputs:
            recipient = visible_email_inputs[0].input_value().strip()
            if not email_pattern.fullmatch(recipient):
                raise CheckoutFlowBlocked(
                    "The preconfigured digital-ticket email is empty or invalid."
                )
        else:
            visible_text = main.inner_text()
            recipients = email_pattern.findall(visible_text)
            if not recipients:
                return False
            if len(set(address.casefold() for address in recipients)) != 1:
                raise CheckoutFlowBlocked(
                    "Several possible ticket recipient addresses are visible; stopping."
                )

        buttons = main.get_by_role("button")
        confirm_buttons = []
        for index in range(buttons.count()):
            button = buttons.nth(index)
            if not button.is_visible() or not button.is_enabled():
                continue
            label = button.get_attribute("aria-label") or button.inner_text()
            folded_label = _normalized_ui_text(label)
            if "confirm" not in folded_label:
                continue
            if re.search(r"trimite|plaseaza|finalizeaza|payment|plata", folded_label):
                continue
            associated = button.evaluate(
                """el => {
                  const emailPattern = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i;
                  const visibleEmailInput = form => [...form.querySelectorAll('input[type="email"]')]
                    .some(input => (input.offsetWidth || input.offsetHeight || input.getClientRects().length)
                      && emailPattern.test((input.value || '').trim()));
                  const form = el.closest('form');
                  if (form && form.contains(el) && visibleEmailInput(form)) return true;
                  let node = el;
                  for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
                    const text = (node.innerText || '').trim();
                    const input = [...node.querySelectorAll('input[type="email"]')]
                      .find(item => item.offsetWidth || item.offsetHeight || item.getClientRects().length);
                    const inputValue = input ? (input.value || '').trim() : '';
                    const recipientLabel = /email|e-mail|bilet|destinatar/i.test(text);
                    if (((inputValue && emailPattern.test(inputValue)) || emailPattern.test(text))
                        && recipientLabel && text.length < 900) return true;
                    if (text.length > 900) break;
                  }
                  return false;
                }"""
            )
            if associated:
                confirm_buttons.append(button)

        if not confirm_buttons:
            # A visible address can also be part of the final checkout review.
            # It is not enough evidence to click an unrelated control.
            return False
        if len(confirm_buttons) != 1:
            raise CheckoutFlowBlocked(
                "The digital-delivery confirmation button is missing or ambiguous."
            )
        confirm_button = confirm_buttons[0]
        confirm_button.click()
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except PlaywrightTimeoutError:
            pass
        self.page.wait_for_timeout(500)
        self._raise_if_login_or_challenge()

        # Do not infer success from the click alone. Require an observable
        # transition in the exact email-confirmation control or recipient field.
        try:
            button_still_visible = confirm_button.is_visible()
            button_still_enabled = confirm_button.is_enabled() if button_still_visible else False
        except PlaywrightError:
            button_still_visible = False
            button_still_enabled = False
        try:
            if visible_email_inputs:
                recipient_still_visible = visible_email_inputs[0].is_visible()
            else:
                recipient_still_visible = bool(email_pattern.search(main.inner_text()))
        except PlaywrightError:
            recipient_still_visible = False
        if button_still_visible and button_still_enabled and recipient_still_visible:
            raise CheckoutFlowBlocked(
                "The digital-ticket recipient confirmation did not show a verifiable state change."
            )
        return True

    def proceed_to_checkout(self, config: ProductConfig, product: ProductObservation) -> CheckoutReview:
        links = self.page.get_by_role(
            "link", name=re.compile(r"continu[ăa]", re.IGNORECASE)
        )
        visible_links = [links.nth(i) for i in range(links.count()) if links.nth(i).is_visible()]
        if len(visible_links) != 1:
            raise CartActionError(
                "The visible cart continuation link is missing or ambiguous."
            )
        link = visible_links[0]
        link.click()
        self.page.wait_for_load_state("domcontentloaded")
        self._raise_if_login_or_challenge()
        host = (urlparse(self.page.url).hostname or "").casefold()
        if host not in {"emag.ro", "www.emag.ro"}:
            raise CartActionError("eMAG did not open the checkout review page.")
        digital_delivery_confirmed = False
        if config.is_digital_ticket:
            digital_delivery_confirmed = self._confirm_digital_delivery_if_present()
        if config.is_digital_ticket and not digital_delivery_confirmed:
            raise CheckoutFlowBlocked(
                "No verified digital-ticket recipient confirmation appeared; automatic ordering is limited to the confirmed ticket flow."
            )
        self._raise_if_login_or_challenge()

        # Record payment labels only; do not read or store card fields or addresses.
        payment_options: list[str] = []
        radio_controls = self.page.locator("main input[type='radio']")
        for index in range(radio_controls.count()):
            radio = radio_controls.nth(index)
            try:
                details = radio.evaluate(
                    """el => ({
                      visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                      label: el.labels ? [...el.labels].map(x => x.innerText.trim()).filter(Boolean).join(' / ') : ''
                    })"""
                )
                label = _safe_payment_method_label(details["label"])
                if details["visible"] and label:
                    payment_options.append(label)
            except PlaywrightError:
                continue
        checkout_summary_info = _visible_order_summary(self.page.locator("main"))
        checkout_summary = str(checkout_summary_info.get("text", ""))
        breakdown = parse_order_total_breakdown(checkout_summary)
        if not breakdown.complete or breakdown.total_ron is None:
            raise CheckoutFlowBlocked(
                "The checkout total or its complete fee breakdown could not be verified."
            )
        return CheckoutReview(
            product_title=product.title,
            quantity=config.quantity,
            cart_total_ron=breakdown.total_ron,
            payment_options=tuple(dict.fromkeys(payment_options)),
            checkout_url=self.page.url,
            product_subtotal_ron=breakdown.product_subtotal_ron,
            digital_delivery_confirmed=digital_delivery_confirmed,
        )

    def proceed_to_checkout_multi(
        self,
        configs: tuple[ProductConfig, ...],
        products: tuple[ProductObservation, ...],
    ) -> CheckoutReview:
        if len(configs) < 2 or len(configs) != len(products):
            raise CheckoutFlowBlocked("The shared checkout request is incomplete.")
        links = self.page.get_by_role(
            "link", name=re.compile(r"continu[ăa]", re.IGNORECASE)
        )
        visible_links = [links.nth(index) for index in range(links.count()) if links.nth(index).is_visible()]
        if len(visible_links) != 1:
            raise CheckoutFlowBlocked(
                "The visible shared-cart continuation link is missing or ambiguous."
            )
        visible_links[0].click()
        self.page.wait_for_load_state("domcontentloaded")
        self._raise_if_login_or_challenge()
        host = (urlparse(self.page.url).hostname or "").casefold()
        if host not in {"emag.ro", "www.emag.ro"}:
            raise CheckoutFlowBlocked("eMAG did not open the shared checkout page.")

        all_digital_tickets = all(config.is_digital_ticket for config in configs)
        digital_delivery_confirmed = False
        if all_digital_tickets:
            digital_delivery_confirmed = self._confirm_digital_delivery_if_present()
            if not digital_delivery_confirmed:
                raise CheckoutFlowBlocked(
                    "The shared ticket order has no verified recipient confirmation."
                )

        self._raise_if_login_or_challenge()
        main = self.page.locator("main")
        visible_text = " ".join(main.inner_text().casefold().split())
        missing_titles = [
            product.title
            for product in products
            if " ".join(product.title.casefold().split()) not in visible_text
        ]
        if missing_titles:
            raise CheckoutFlowBlocked(
                "One or more configured products are not visible in the shared checkout summary."
            )

        payment_options: list[str] = []
        radio_controls = main.locator("input[type='radio']")
        for index in range(radio_controls.count()):
            radio = radio_controls.nth(index)
            try:
                details = radio.evaluate(
                    """el => ({
                      visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                      label: el.labels ? [...el.labels].map(x => x.innerText.trim()).filter(Boolean).join(' / ') : ''
                    })"""
                )
                label = _safe_payment_method_label(details["label"])
                if details["visible"] and label:
                    payment_options.append(label)
            except PlaywrightError:
                continue

        summary = _visible_order_summary(main)
        breakdown = parse_order_total_breakdown(str(summary.get("text", "")))
        if not breakdown.complete or breakdown.total_ron is None:
            raise CheckoutFlowBlocked(
                "The shared checkout total or its complete fee breakdown could not be verified."
            )
        return CheckoutReview(
            product_title=f"Shared cart ({len(products)} products)",
            quantity=sum(config.quantity for config in configs),
            cart_total_ron=breakdown.total_ron,
            payment_options=tuple(dict.fromkeys(payment_options)),
            checkout_url=self.page.url,
            product_subtotal_ron=breakdown.product_subtotal_ron,
            digital_delivery_confirmed=digital_delivery_confirmed,
        )

    def validate_order_submission(
        self,
        expected_total_ron: Decimal,
        *,
        expected_product_subtotal_ron: Decimal | None = None,
        expected_product_titles: tuple[str, ...] = (),
    ) -> None:
        """Recheck the visible payable total and final button before any order click."""
        self._raise_if_login_or_challenge()
        host = (urlparse(self.page.url).hostname or "").casefold()
        if host not in {"emag.ro", "www.emag.ro"} or "/cart/checkout" not in self.page.url:
            raise CartActionError("The browser is not on the verified eMAG checkout page.")
        summary = _visible_order_summary(self.page.locator("main"))
        breakdown = parse_order_total_breakdown(str(summary.get("text", "")))
        if not breakdown.complete or breakdown.total_ron is None:
            raise CartActionError(
                "The checkout fee breakdown is incomplete; automatic order stopped."
            )
        if breakdown.total_ron != expected_total_ron:
            raise CartActionError(
                "The checkout payable total changed after review; automatic order stopped."
            )
        if (
            expected_product_subtotal_ron is not None
            and breakdown.product_subtotal_ron != expected_product_subtotal_ron
        ):
            raise CartActionError(
                "The checkout product subtotal changed after review; automatic order stopped."
            )
        if breakdown.total_ron > Decimal("100.00"):
            raise CartActionError(
                "The checkout payable total exceeds the hard 100.00 RON limit."
            )
        visible_checkout_text = _normalized_ui_text(
            self.page.locator("main").inner_text()
        )
        if any(
            _normalized_ui_text(title) not in visible_checkout_text
            for title in expected_product_titles
        ):
            raise CartActionError(
                "One or more expected products are no longer visible in the checkout."
            )
        buttons = self.page.get_by_role(
            "button", name=re.compile(r"^trimite\s+comanda$", re.IGNORECASE)
        )
        visible = [
            buttons.nth(index)
            for index in range(buttons.count())
            if buttons.nth(index).is_visible()
        ]
        if len(visible) != 1 or not visible[0].is_enabled():
            raise CartActionError(
                "The final checkout button is missing, disabled, or ambiguous."
            )
        self._raise_if_manual_payment_challenge()

    def _raise_if_manual_payment_challenge(self) -> None:
        try:
            body = self.page.locator("body").inner_text(timeout=2_000)
        except PlaywrightError:
            return
        folded = _normalized_ui_text(body)
        if any(
            marker in folded
            for marker in (
                "3-d secure",
                "3d secure",
                "autentificare suplimentara",
                "confirmarea platii",
                "confirmati plata",
                "secure authentication",
            )
        ):
            raise CaptchaRequired(
                "A payment security challenge is present. Complete it manually; the app will not automate it."
            )

    def submit_order_once(
        self,
        expected_total_ron: Decimal,
        *,
        stop_event=None,
        expected_product_subtotal_ron: Decimal | None = None,
        expected_product_titles: tuple[str, ...] = (),
    ) -> EmagOrderResult:
        """Click the uniquely named final order button once, then verify visible outcome."""
        self.validate_order_submission(
            expected_total_ron,
            expected_product_subtotal_ron=expected_product_subtotal_ron,
            expected_product_titles=expected_product_titles,
        )
        if stop_event is not None and stop_event.is_set():
            return EmagOrderResult(
                "cancelled_before_click",
                "The workflow was stopped before the final order button was clicked.",
            )
        buttons = self.page.get_by_role(
            "button", name=re.compile(r"^trimite\s+comanda$", re.IGNORECASE)
        )
        button = buttons.nth(0)
        try:
            button.click(timeout=8_000)
        except PlaywrightError:
            return EmagOrderResult(
                "order_status_unknown",
                "The order button was activated or timed out ambiguously. Do not retry; verify the eMAG order history manually.",
            )

        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=8_000)
        except PlaywrightTimeoutError:
            pass
        try:
            self.page.wait_for_timeout(1_500)
            self._raise_if_login_or_challenge()
        except CaptchaRequired:
            return EmagOrderResult(
                "manual_payment_or_security_step",
                "A security check appeared after the order action. Finish it manually and verify the order; the app will not retry.",
            )
        except PlaywrightError:
            return EmagOrderResult(
                "order_status_unknown",
                "The order action returned no verifiable page state. Do not retry; check the eMAG order history manually.",
            )

        try:
            main = self.page.locator("main")
            headings = main.get_by_role("heading")
            heading_text = " ".join(
                headings.nth(index).inner_text().strip()
                for index in range(headings.count())
                if headings.nth(index).is_visible()
            )
            folded_headings = _normalized_ui_text(heading_text)
            on_checkout = "/cart/checkout" in self.page.url
            confirmed = any(
                phrase in folded_headings
                for phrase in (
                    "comanda a fost plasata",
                    "comanda ta a fost plasata",
                    "multumim pentru comanda",
                    "comanda a fost inregistrata",
                )
            )
            if confirmed and not on_checkout:
                return EmagOrderResult(
                    "order_placed_verified",
                    "eMAG displayed an order confirmation page after submission.",
                )
        except PlaywrightError:
            pass
        return EmagOrderResult(
            "order_status_unknown",
            "The order action was sent, but the page did not show a recognized confirmation. Do not retry; verify eMAG order history manually.",
        )
