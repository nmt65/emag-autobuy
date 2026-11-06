import threading
import unittest
from datetime import datetime
from decimal import Decimal

from browser.emag import (
    CartActionError,
    CartSnapshot,
    CheckoutFlowBlocked,
    EmagPage,
    PageStructureChanged,
    _safe_payment_method_label,
    _visible_order_summary,
)
from core.conditions import ProductConfig, ProductObservation, parse_order_total_breakdown

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - requirement installation supplies Playwright
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class VisibleDomSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:
            cls.playwright.stop()
            raise unittest.SkipTest(f"Install Chromium with playwright install chromium: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.adapter = EmagPage(self.page)

    def tearDown(self):
        self.page.close()

    def checkout_adapter(self):
        class CheckoutPageProxy:
            def __init__(self, page):
                self._page = page

            @property
            def url(self):
                confirmation = self._page.locator("#order-confirmed")
                if confirmation.count() and confirmation.inner_text().strip():
                    return "https://www.emag.ro/order/confirmation"
                return "https://www.emag.ro/cart/checkout"

            def __getattr__(self, name):
                return getattr(self._page, name)

        return EmagPage(CheckoutPageProxy(self.page))

    @staticmethod
    def checkout_html(total: str = "100,00") -> str:
        if total == "100,01":
            delivery = "4,02"
        else:
            delivery = "4,01"
        return f"""<main>
          <h2>Detalii comandă</h2>
          <section><span>Sumar comandă</span>
            <p>Cost produse: 95,00 Lei</p>
            <p>Servicii operationale: 0,99 Lei</p>
            <p>Cost livrare: {delivery} Lei</p>
            <p>Total: {total} Lei</p>
          </section>
          <button id="submit" onclick="window.clickCount=(window.clickCount||0)+1; document.querySelector('#order-confirmed').textContent='Comanda a fost plasată cu succes'">Trimite comanda</button>
          <h1 id="order-confirmed"></h1>
        </main>"""

    def test_product_price_reads_current_price_not_crossed_out_prp(self):
        self.page.set_content(
            """<main>
              <h1 class="page-title">Sample product</h1>
              <p class="pricing rrp-lp30d"><s>PRP: 393,38 Lei</s></p>
              <p data-test="main-price">219,17 Lei</p>
              <span class="label label-in_stock">În stoc</span>
              <button data-test="main-add-to-cart-button">Adauga in Cos</button>
            </main>"""
        )
        product = self.adapter.read_product()
        self.assertEqual(product.price_ron, Decimal("219.17"))
        self.assertTrue(product.in_stock)
        self.assertTrue(product.can_purchase)

    def test_payment_review_keeps_generic_method_but_discards_card_token(self):
        self.assertEqual(_safe_payment_method_label("Card online"), "Card online")
        self.assertIsNone(_safe_payment_method_label("VISA •••• 0038"))
        self.assertIsNone(_safe_payment_method_label("Card ending 0038"))

    def test_digital_delivery_confirms_one_prefilled_email_without_persisting_it(self):
        self.page.set_content(
            """<main><form>
              <label>Email pentru bilete<input type="email" value="tickets@example.test"></label>
              <button type="button" onclick="this.dataset.confirmed='yes';this.disabled=true">Confirmă</button>
            </form></main>"""
        )
        self.assertTrue(self.adapter._confirm_digital_delivery_if_present())
        self.assertEqual(
            self.page.get_by_role("button", name="Confirmă").get_attribute("data-confirmed"),
            "yes",
        )

    def test_digital_delivery_does_not_click_unassociated_confirm_button(self):
        self.page.set_content(
            """<main><form><input type="email" value="tickets@example.test"></form>
              <button type="button" onclick="this.dataset.confirmed='yes'">Confirmă</button>
            </main>"""
        )
        self.assertFalse(self.adapter._confirm_digital_delivery_if_present())
        self.assertIsNone(
            self.page.get_by_role("button", name="Confirmă").get_attribute("data-confirmed")
        )

    def test_digital_delivery_requires_a_visible_confirmation_transition(self):
        self.page.set_content(
            """<main><form>
              <label>Email pentru bilete<input type="email" value="tickets@example.test"></label>
              <button type="button" onclick="window.clickCount=(window.clickCount||0)+1">Confirmă</button>
            </form></main>"""
        )
        with self.assertRaisesRegex(
            CheckoutFlowBlocked, "did not show a verifiable state change"
        ):
            self.adapter._confirm_digital_delivery_if_present()
        self.assertEqual(self.page.evaluate("window.clickCount"), 1)

    def test_final_order_button_is_clicked_once_after_exact_100_ron_recheck(self):
        self.page.set_content(self.checkout_html("100,00"))
        adapter = self.checkout_adapter()
        adapter.validate_order_submission(Decimal("100.00"))
        result = adapter.submit_order_once(Decimal("100.00"))
        self.assertEqual(result.status, "order_placed_verified")
        self.assertEqual(self.page.evaluate("window.clickCount"), 1)

    def test_final_order_button_is_not_clicked_above_100_ron(self):
        self.page.set_content(self.checkout_html("100,01"))
        adapter = self.checkout_adapter()
        with self.assertRaisesRegex(CartActionError, "hard 100.00 RON limit"):
            adapter.validate_order_submission(Decimal("100.01"))
        self.assertEqual(self.page.evaluate("window.clickCount || 0"), 0)

    def test_checkout_product_subtotal_change_stops_before_the_final_click(self):
        self.page.set_content(self.checkout_html("100,00"))
        adapter = self.checkout_adapter()
        with self.assertRaisesRegex(CartActionError, "product subtotal changed"):
            adapter.validate_order_submission(
                Decimal("100.00"),
                expected_product_subtotal_ron=Decimal("94.00"),
            )
        self.assertEqual(self.page.evaluate("window.clickCount || 0"), 0)

    def test_final_order_button_with_an_unexpected_label_is_rejected(self):
        self.page.set_content(
            self.checkout_html("100,00").replace(
                "Trimite comanda</button>", "Trimite comanda acum</button>"
            )
        )
        adapter = self.checkout_adapter()
        with self.assertRaisesRegex(CartActionError, "missing, disabled, or ambiguous"):
            adapter.validate_order_submission(Decimal("100.00"))
        self.assertEqual(self.page.evaluate("window.clickCount || 0"), 0)

    def test_stop_event_blocks_the_final_order_click(self):
        import threading

        self.page.set_content(self.checkout_html("100,00"))
        adapter = self.checkout_adapter()
        stopped = threading.Event()
        stopped.set()
        result = adapter.submit_order_once(Decimal("100.00"), stop_event=stopped)
        self.assertEqual(result.status, "cancelled_before_click")
        self.assertEqual(self.page.evaluate("window.clickCount || 0"), 0)

    def test_duplicate_current_price_fails_closed(self):
        self.page.set_content(
            """<main>
              <h1 class="page-title">Sample product</h1>
              <p data-test="main-price">219,17 Lei</p>
              <p data-test="main-price">199,17 Lei</p>
              <span class="label label-in_stock">În stoc</span>
              <button data-test="main-add-to-cart-button">Adauga in Cos</button>
            </main>"""
        )
        self.assertIsNone(self.adapter.read_product().price_ron)

    def test_duplicate_product_title_fails_closed(self):
        self.page.set_content(
            """<main><h1 class="page-title">Sample product</h1>
              <h1 class="page-title">Another product</h1></main>"""
        )
        with self.assertRaises(PageStructureChanged):
            self.adapter.read_product()

    def test_cart_row_and_fee_summary_are_parsed_from_visible_semantics(self):
        self.page.set_content(
            """<main>
              <h1>Coșul meu</h1>
              <article>
                <h2>Sample product</h2><p>100,00 Lei</p>
                <label for="qty">Cantitate</label>
                <input id="qty" name="quantity" type="number" min="1" max="3" value="2">
              </article>
              <section>
                <h2>Sumar comandă</h2>
                <p>Cost produse: 200,00 Lei</p>
                <p>Cost livrare: Gratuit</p>
                <p>Taxă serviciu: 5,00 Lei</p>
                <p>Total: 205,00 Lei</p>
              </section>
              <a href="#">Continuă</a>
            </main>"""
        )
        snapshot = self.adapter._cart_snapshot("Sample product")
        self.assertTrue(snapshot.product_present)
        self.assertEqual(snapshot.quantity, 2)
        self.assertEqual(snapshot.quantity_field.minimum, 1)
        self.assertEqual(snapshot.quantity_field.maximum, 3)
        self.assertEqual(snapshot.cart_unit_price_ron, Decimal("100.00"))
        self.assertEqual(snapshot.product_subtotal_ron, Decimal("200.00"))
        self.assertEqual(snapshot.cart_total_ron, Decimal("205.00"))

    def test_two_product_shared_cart_rows_reconcile_one_subtotal_and_total(self):
        self.page.set_content(
            """<main>
              <h1>Coșul meu</h1>
              <article><h2>Digital ticket alpha</h2><p>40,00 Lei</p>
                <div role="group" aria-label="Cantitate"><button disabled>Scade cantitatea</button>
                  <span>1</span><button>Crește cantitatea</button></div></article>
              <article><h2>Digital ticket beta</h2><p>45,00 Lei</p>
                <div role="group" aria-label="Cantitate"><button disabled>Scade cantitatea</button>
                  <span>1</span><button>Crește cantitatea</button></div></article>
              <section><h2>Sumar comandă</h2>
                <p>Cost produse: 85,00 Lei</p><p>Cost livrare: 14,01 Lei</p>
                <p>Servicii operationale: 0,99 Lei</p><p>Total: 100,00 Lei</p>
              </section>
            </main>"""
        )
        configs = tuple(
            ProductConfig(
                product_url=f"https://www.emag.ro/ticket/pd/{code}/",
                max_price_ron=Decimal("50.00"),
                quantity=1,
                target_date="2026-11-06",
                target_time="12:00:00",
                max_attempts=1,
            )
            for code in ("AA11BB2BM", "CC33DD4BM")
        )
        products = (
            ProductObservation(
                title="Digital ticket alpha", price_ron=Decimal("40.00"), in_stock=True,
                can_purchase=True, page_url=configs[0].product_url,
                observed_at=datetime.now().astimezone(),
            ),
            ProductObservation(
                title="Digital ticket beta", price_ron=Decimal("45.00"), in_stock=True,
                can_purchase=True, page_url=configs[1].product_url,
                observed_at=datetime.now().astimezone(),
            ),
        )
        snapshots = self.adapter._verified_multi_cart_rows(
            configs, products, require_requested_quantities=True
        )
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(all(snapshot.product_present for snapshot in snapshots))
        self.assertEqual(snapshots[0].product_subtotal_ron, Decimal("85.00"))
        self.assertEqual(snapshots[0].cart_total_ron, Decimal("100.00"))

    def test_shared_checkout_requires_every_expected_product_and_one_total(self):
        self.page.set_content(
            """<main>
              <a href="#checkout">Continuă</a>
              <h2>Digital ticket alpha</h2><p>Cantitate 1</p>
              <h2>Digital ticket beta</h2><p>Cantitate 1</p>
              <section><h2>Sumar comandă</h2>
                <p>Cost produse: 85,00 Lei</p><p>Cost livrare: Gratuit</p>
                <p>Taxă servicii: 0,99 Lei</p><p>Total: 85,99 Lei</p>
              </section>
            </main>"""
        )
        configs = tuple(
            ProductConfig(
                product_url=f"https://www.emag.ro/ticket/pd/{code}/",
                max_price_ron=Decimal("50.00"), quantity=1,
                target_date="2026-11-06", target_time="12:00:00", max_attempts=1,
            )
            for code in ("AA11BB2BM", "CC33DD4BM")
        )
        products = tuple(
            ProductObservation(
                title=title, price_ron=price, in_stock=True, can_purchase=True,
                page_url=config.product_url, observed_at=datetime.now().astimezone(),
            )
            for config, title, price in zip(
                configs,
                ("Digital ticket alpha", "Digital ticket beta"),
                (Decimal("40.00"), Decimal("45.00")),
                strict=True,
            )
        )
        review = self.checkout_adapter().proceed_to_checkout_multi(configs, products)
        self.assertEqual(review.quantity, 2)
        self.assertEqual(review.product_subtotal_ron, Decimal("85.00"))
        self.assertEqual(review.cart_total_ron, Decimal("85.99"))

        self.page.set_content(
            """<main><a href="#checkout">Continuă</a>
              <h2>Digital ticket alpha</h2>
              <section><h2>Sumar comandă</h2><p>Cost produse: 40,00 Lei</p>
                <p>Cost livrare: Gratuit</p><p>Total: 40,00 Lei</p></section></main>"""
        )
        with self.assertRaisesRegex(CheckoutFlowBlocked, "not visible"):
            self.checkout_adapter().proceed_to_checkout_multi(configs, products)

    def test_shared_cart_rejects_an_unconfigured_extra_product_row(self):
        self.page.set_content(
            """<main>
              <article><h2>Digital ticket alpha</h2><p>40,00 Lei</p>
                <div role="group" aria-label="Cantitate"><button disabled>Scade cantitatea</button>
                  <span>1</span><button>Crește cantitatea</button></div></article>
              <article><h2>Digital ticket beta</h2><p>45,00 Lei</p>
                <div role="group" aria-label="Cantitate"><button disabled>Scade cantitatea</button>
                  <span>1</span><button>Crește cantitatea</button></div></article>
              <article><h2>Other product</h2><p>1,00 Lei</p>
                <div role="group" aria-label="Cantitate"><button disabled>Scade cantitatea</button>
                  <span>1</span><button>Crește cantitatea</button></div></article>
              <section><h2>Sumar comandă</h2>
                <p>Cost produse: 86,00 Lei</p><p>Cost livrare: Gratuit</p>
                <p>Total: 86,00 Lei</p></section>
            </main>"""
        )
        configs = tuple(
            ProductConfig(
                product_url=f"https://www.emag.ro/ticket/pd/{code}/",
                max_price_ron=Decimal("50.00"), quantity=1,
                target_date="2026-11-06", target_time="12:00:00", max_attempts=1,
            )
            for code in ("AA11BB2BM", "CC33DD4BM")
        )
        products = tuple(
            ProductObservation(
                title=title, price_ron=price, in_stock=True, can_purchase=True,
                page_url=config.product_url, observed_at=datetime.now().astimezone(),
            )
            for config, title, price in zip(
                configs,
                ("Digital ticket alpha", "Digital ticket beta"),
                (Decimal("40.00"), Decimal("45.00")),
                strict=True,
            )
        )
        with self.assertRaisesRegex(CartActionError, "exactly one visible quantity row"):
            self.adapter._verified_multi_cart_rows(
                configs, products, require_requested_quantities=True
            )

    def test_multi_cart_preparation_adds_each_product_once_and_verifies_shared_rows(self):
        class SharedCartPageProxy:
            def __init__(self, page):
                self.page = page
                self.url = "https://www.emag.ro/"
                self.items = []
                self.products = {
                    "AA11BB2BM": ("Digital ticket alpha", Decimal("40.00")),
                    "CC33DD4BM": ("Digital ticket beta", Decimal("45.00")),
                }
                self.render_cart()

            def __getattr__(self, name):
                return getattr(self.page, name)

            @staticmethod
            def ron(value):
                return f"{value:.2f}".replace(".", ",")

            def goto(self, url, **_kwargs):
                if "/cart/products" in url:
                    pending = self.page.evaluate("window.pendingProduct || null")
                    if pending:
                        self.items.append(pending)
                    self.url = url
                    self.render_cart()
                    return
                code = url.split("/pd/")[-1].split("/")[0].upper()
                title, price = self.products[code]
                self.url = url
                self.page.set_content(
                    f"""<main><h1 class="page-title">{title}</h1>
                      <p data-test="main-price">{self.ron(price)} Lei</p>
                      <span class="label label-in_stock">În stoc</span>
                      <button data-test="main-add-to-cart-button"
                        onclick="window.pendingProduct='{code}'">Adaugă în coș</button>
                    </main>"""
                )

            def render_cart(self):
                rows = []
                subtotal = Decimal("0.00")
                for code in self.items:
                    title, price = self.products[code]
                    subtotal += price
                    rows.append(
                        f"""<article><h2>{title}</h2><p>{self.ron(price)} Lei</p>
                          <div role="group" aria-label="Cantitate">
                            <button disabled>Scade cantitatea</button><span>1</span>
                            <button>Crește cantitatea</button>
                          </div></article>"""
                    )
                if not rows:
                    rows = ["<h2>Produse alese pentru tine</h2>"]
                total = subtotal
                self.page.set_content(
                    "<main><h1>Coșul meu</h1>"
                    + "".join(rows)
                    + (
                        f"<section><h2>Sumar comandă</h2>"
                        f"<p>Cost produse: {self.ron(subtotal)} Lei</p>"
                        f"<p>Cost livrare: Gratuit</p><p>Total: {self.ron(total)} Lei</p></section>"
                        if self.items
                        else ""
                    )
                    + "</main>"
                )

        proxy = SharedCartPageProxy(self.page)
        adapter = EmagPage(proxy)
        configs = tuple(
            ProductConfig(
                product_url=f"https://www.emag.ro/ticket/pd/{code}/",
                max_price_ron=Decimal("50.00"),
                quantity=1,
                target_date="2026-11-06",
                target_time="12:00:00",
                max_attempts=1,
            )
            for code in ("AA11BB2BM", "CC33DD4BM")
        )
        observations = tuple(
            ProductObservation(
                title=proxy.products[code][0],
                price_ron=proxy.products[code][1],
                in_stock=True,
                can_purchase=True,
                page_url=config.product_url,
                observed_at=datetime.now().astimezone(),
            )
            for config, code in zip(configs, ("AA11BB2BM", "CC33DD4BM"), strict=True)
        )
        snapshots = adapter.prepare_multi_cart(configs, observations)
        self.assertEqual(proxy.items, ["AA11BB2BM", "CC33DD4BM"])
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0].product_subtotal_ron, Decimal("85.00"))
        self.assertEqual(snapshots[0].cart_total_ron, Decimal("85.00"))

    def test_live_cart_shape_uses_quantity_stepper_and_plain_text_summary(self):
        self.page.set_content(
            """<main>
              <h1>Coșul meu</h1>
              <article>
                <h2>Periuta de dinti Colgate Classic Clean Hard</h2>
                <p>3,38 Lei</p>
                <div role="group" aria-label="Cantitate">
                  <button disabled>Scade cantitatea</button>
                  <span id="quantity">1</span>
                  <button onclick="document.querySelector('#quantity').textContent = '2'">Crește cantitatea</button>
                </div>
              </article>
              <div><span>Sumar comandă</span>
                <div>Cost produse: 3,38 Lei</div>
                <div>Cost livrare: 12,99 Lei</div>
                <div>Total: 16,37 Lei</div>
              </div>
              <section><p>Donație: 5,00 Lei</p></section>
            </main>"""
        )
        snapshot = self.adapter._cart_snapshot(
            "Periuta de dinti Colgate Classic Clean Hard"
        )
        self.assertTrue(snapshot.product_present)
        self.assertEqual(snapshot.quantity_field.control_type, "button-group")
        self.assertEqual(snapshot.quantity, 1)
        self.assertEqual(snapshot.cart_unit_price_ron, Decimal("3.38"))
        self.assertEqual(snapshot.product_subtotal_ron, Decimal("3.38"))
        self.assertEqual(snapshot.cart_total_ron, Decimal("16.37"))
        self.adapter._set_quantity(snapshot.quantity_field, 2)
        self.assertEqual(
            self.adapter._read_quantity_group(
                self.page.get_by_role("group", name="Cantitate")
            ),
            2,
        )

    def test_duplicate_plain_text_order_summary_fails_closed(self):
        self.page.set_content(
            """<main>
              <article><h2>Sample product</h2><p>10,00 Lei</p>
                <label for="qty">Cantitate</label>
                <input id="qty" name="quantity" type="number" value="1">
              </article>
              <div><span>Sumar comandă</span><p>Cost produse: 10,00 Lei</p>
                <p>Cost livrare: Gratuit</p><p>Total: 10,00 Lei</p></div>
              <div><span>Sumar comandă</span></div>
            </main>"""
        )
        snapshot = self.adapter._cart_snapshot("Sample product")
        self.assertIsNone(snapshot.cart_total_ron)

    def test_quantity_stepper_stops_before_excessive_clicks(self):
        self.page.set_content(
            """<main><div role="group" aria-label="Cantitate">
              <button>Scade cantitatea</button><span>1</span>
              <button>Crește cantitatea</button>
            </div></main>"""
        )
        with self.assertRaisesRegex(CartActionError, "more than 10"):
            self.adapter._set_quantity(
                type(
                    "QuantityFieldFixture",
                    (),
                    {"control_type": "button-group", "element_index": 0, "value": "1"},
                )(),
                12,
            )

    def test_checkout_summary_scope_includes_operational_fee_not_page_terms(self):
        self.page.set_content(
            """<main>
              <div><span>Sumar comandă</span>
                <div>Cost produse:<span>3</span><span>,</span><span>38</span> Lei</div>
                <div>Servicii operationale: 0,99 Lei</div>
                <div>Cost livrare: 12,99 Lei</div>
                <div>Total: 17,36 Lei</div>
              </div>
              <p>Prin plasarea comenzii, ești de acord cu termenii.</p>
            </main>"""
        )
        summary = _visible_order_summary(self.page.locator("main"))
        self.assertEqual(summary["markerCount"], 1)
        breakdown = parse_order_total_breakdown(summary["text"])
        self.assertTrue(breakdown.complete, breakdown.reasons)
        self.assertEqual(breakdown.total_ron, Decimal("17.36"))

    def test_cart_unit_price_does_not_choose_between_crossed_out_and_current_values(self):
        self.page.set_content(
            """<main>
              <article><h2>Sample product</h2>
                <p><s>PRP: 20,00 Lei</s></p><p>10,00 Lei</p>
                <label for="qty">Cantitate</label>
                <input id="qty" name="quantity" type="number" value="1">
              </article>
              <section><h2>Sumar comandă</h2>
                <p>Cost produse: 10,00 Lei</p>
                <p>Cost livrare: Gratuit</p><p>Total: 10,00 Lei</p>
              </section>
            </main>"""
        )
        snapshot = self.adapter._cart_snapshot("Sample product")
        self.assertTrue(snapshot.product_present)
        self.assertIsNone(snapshot.cart_unit_price_ron)

    def test_observed_empty_cart_recommendations_are_not_treated_as_cart_line(self):
        self.page.set_content(
            """<main>
              <h1>Coșul meu</h1>
              <h2>Produse alese pentru tine</h2>
              <article><h3>Sample product</h3><p>100,00 Lei</p>
                <button>Adauga in Cos</button></article>
            </main>"""
        )
        snapshot = self.adapter._cart_snapshot("Sample product")
        self.assertFalse(snapshot.product_present)
        self.assertTrue(snapshot.cart_known_empty)
        self.assertFalse(snapshot.ambiguous_product_reference)

    def test_cart_without_observed_empty_state_stays_ambiguous(self):
        self.page.set_content(
            """<main><h1>Coșul meu</h1><h2>Sample product</h2>
              <p>100,00 Lei</p></main>"""
        )
        snapshot = self.adapter._cart_snapshot("Sample product")
        self.assertFalse(snapshot.cart_known_empty)
        self.assertTrue(snapshot.ambiguous_product_reference)

    def test_unconfirmed_empty_cart_stops_before_add_to_cart(self):
        config = ProductConfig(
            product_url="https://www.emag.ro/item/pd/AB12CD3BM/",
            max_price_ron=Decimal("100.00"),
            quantity=1,
            target_date="2026-11-06",
            target_time="12:00:00",
            max_attempts=1,
        )
        observation = ProductObservation(
            title="Sample product",
            price_ron=Decimal("100.00"),
            in_stock=True,
            can_purchase=True,
            page_url=config.product_url,
            observed_at=datetime.now().astimezone(),
        )

        class EmptyCartAdapter(EmagPage):
            def __init__(self):
                self.page = None
                self.opens = 0

            def open_product(self, _url):
                self.opens += 1

            def read_product(self, _url=None):
                return observation

            def inspect_cart(self, _title):
                return CartSnapshot(
                    visible_text="Coșul meu\nProduse alese pentru tine",
                    product_present=False,
                    cart_unit_price_ron=None,
                    quantity_field=None,
                    quantity=None,
                    cart_total_ron=None,
                    product_subtotal_ron=None,
                    cart_known_empty=False,
                    ambiguous_product_reference=False,
                    quantity_control_count=0,
                )

        adapter = EmptyCartAdapter()
        with self.assertRaisesRegex(CartActionError, "will not add another item"):
            adapter.prepare_cart(config, observation)
        self.assertEqual(adapter.opens, 1)


if __name__ == "__main__":
    unittest.main()
