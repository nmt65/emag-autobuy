from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

import streamlit as st

from browser.manager import validate_cdp_endpoint
from core.conditions import ConfigurationError, ProductConfig
from core.config import AppSettings, load_settings, save_settings
from core.runtime import WorkerController
from database.db import Database


MAX_PRODUCTS = 10

st.set_page_config(page_title="eMAG AutoBuy", page_icon="🛒", layout="wide")


@st.cache_resource
def get_controller() -> WorkerController:
    return WorkerController()


@st.cache_resource
def get_database(path: str) -> Database:
    return Database(path)


def _draft_row(product: ProductConfig) -> dict:
    return {
        "_key": uuid4().hex,
        "product_url": product.product_url,
        "max_price_ron": str(product.max_price_ron),
        "quantity": product.quantity,
        "target_date": product.target_date,
        "target_time": product.target_time,
        "max_attempts": product.max_attempts,
        "must_be_in_stock": product.must_be_in_stock,
        "is_digital_ticket": product.is_digital_ticket,
        "poll_interval_seconds": product.poll_interval_seconds,
        "deadline_minutes_after_target": product.deadline_minutes_after_target,
    }


def _parse_product_row(row: dict) -> ProductConfig:
    try:
        price = Decimal(str(row["max_price_ron"])).quantize(Decimal("0.01"))
        target_date = date.fromisoformat(str(row["target_date"]))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ConfigurationError("Enter a valid RON price and target date for each product.") from exc
    product = ProductConfig(
        product_url=str(row["product_url"]).strip(),
        max_price_ron=price,
        quantity=int(row["quantity"]),
        target_date=target_date.isoformat(),
        target_time=str(row["target_time"]).strip(),
        max_attempts=int(row["max_attempts"]),
        must_be_in_stock=bool(row["must_be_in_stock"]),
        is_digital_ticket=bool(row.get("is_digital_ticket", False)),
        poll_interval_seconds=int(row["poll_interval_seconds"]),
        deadline_minutes_after_target=int(row["deadline_minutes_after_target"]),
        timezone_name="Europe/Bucharest",
    )
    product.validate()
    return product


def _settings_with_products(
    current: AppSettings,
    rows: list[dict],
    *,
    browser_mode: str,
    browser_cdp_endpoint: str,
) -> AppSettings:
    if browser_mode not in {"persistent", "chrome_cdp"}:
        raise ConfigurationError("Alege un mod de browser disponibil.")
    if browser_mode == "chrome_cdp":
        try:
            browser_cdp_endpoint = validate_cdp_endpoint(browser_cdp_endpoint)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
    products = tuple(_parse_product_row(row) for row in rows)
    if len(products) > MAX_PRODUCTS:
        raise ConfigurationError(f"You can configure at most {MAX_PRODUCTS} products.")
    codes: set[str] = set()
    for product in products:
        match = re.search(r"/pd/([^/?#]+)", product.product_url, re.IGNORECASE)
        if match is None:
            raise ConfigurationError("Every URL must contain a valid eMAG product code.")
        code = match.group(1).casefold()
        if code in codes:
            raise ConfigurationError("Each product can appear only once in the list.")
        codes.add(code)
    return AppSettings(
        product=products[0],
        additional_products=products[1:],
        timezone_name="Europe/Bucharest",
        poll_interval_seconds=current.poll_interval_seconds,
        deadline_minutes_after_target=current.deadline_minutes_after_target,
        ntp_servers=current.ntp_servers,
        browser_profile_dir=current.browser_profile_dir,
        browser_mode=browser_mode,
        browser_cdp_endpoint=browser_cdp_endpoint,
        database_path=current.database_path,
    )


def _time_until_target(target_value: str | None) -> str:
    if not target_value:
        return "—"
    try:
        target = datetime.fromisoformat(target_value)
        now = datetime.now(target.tzinfo)
        seconds = int((target - now).total_seconds())
    except (ValueError, TypeError):
        return "Oră invalidă"
    if seconds <= 0:
        return "Ora țintă trecută"
    return f"{seconds // 3600:02}:{(seconds % 3600) // 60:02}:{seconds % 60:02}"


settings = load_settings()
database_path = str(settings.resolved_path(settings.database_path))
database = get_database(database_path)
controller = get_controller()

if "draft_products" not in st.session_state:
    st.session_state.draft_products = [_draft_row(product) for product in settings.products]
if not st.session_state.draft_products:
    st.session_state.draft_products = [_draft_row(settings.product)]

st.title("eMAG AutoBuy")
st.caption("Monitorizare programată pentru produse eMAG, cu stare și jurnal separate.")
st.info(
    "Poți monitoriza simultan până la 10 produse. Verificările folosesc o singură fereastră "
    "vizibilă, serializată, pentru a evita amestecarea sesiunii sau a coșului. În modul de "
    "test, aplicația nu modifică coșul. În modul live, toate produsele trebuie să îndeplinească "
    "condițiile; apoi sunt adăugate serial în coșul comun și verificate ca o singură comandă. "
    "Dacă produsul nu se califică, fluxul nu începe coșul; dacă un rând sau total nu poate fi "
    "verificat, browserul rămâne deschis pentru verificarea ta."
)
st.warning(
    "Modul Test este activ implicit. Dacă îl dezactivezi, fluxul live "
    "poate trimite automat comanda numai dacă fiecare produs este bifat explicit ca bilet digital, "
    "destinatarul este confirmat vizibil și totalul complet al coșului comun (cu taxe și livrare) "
    "este cel mult 100 RON. Pentru produse mixte, valori ambigue sau total peste plafon, "
    "automatizarea se oprește și lasă browserul deschis pentru verificarea ta. "
    "Nu reîncearcă o comandă cu rezultat incert. CAPTCHA și 3-D Secure rămân pași manuali. "
    "Comenzile cu mai multe produse folosesc un singur total comun."
)

st.subheader("Browser pentru eMAG")
browser_modes = {
    "Profil Playwright separat": "persistent",
    "Chrome existent, deja autentificat (localhost CDP)": "chrome_cdp",
}
current_mode_label = next(
    (label for label, mode in browser_modes.items() if mode == settings.browser_mode),
    "Profil Playwright separat",
)
browser_mode_label = st.selectbox(
    "Sesiune browser",
    options=list(browser_modes),
    index=list(browser_modes).index(current_mode_label),
    key="browser_mode",
)
browser_mode = browser_modes[browser_mode_label]
browser_cdp_endpoint = settings.browser_cdp_endpoint
if browser_mode == "chrome_cdp":
    browser_cdp_endpoint = st.text_input(
        "Endpoint Chrome local",
        value=settings.browser_cdp_endpoint,
        help="Acceptă doar localhost, de exemplu http://127.0.0.1:9222. Nu folosi un endpoint de rețea.",
        key="browser_cdp_endpoint",
    )
    st.info(
        "Chrome trebuie să aibă Remote Debugging activat manual. Aplicația folosește un tab nou "
        "în profilul autentificat și nu închide taburile tale. Dacă endpointul nu este disponibil, "
        "fluxul se oprește; nu pornește un browser neautentificat ca rezervă."
    )

add_col, remove_col = st.columns([1, 2])
with add_col:
    if st.button("Adaugă produs", disabled=len(st.session_state.draft_products) >= MAX_PRODUCTS):
        today = datetime.now(ZoneInfo("Europe/Bucharest")).date()
        st.session_state.draft_products.append(
            {
                "_key": uuid4().hex,
                "product_url": "",
                "max_price_ron": "5.00",
                "quantity": 1,
                "target_date": today.isoformat(),
                "target_time": "12:00:00",
                "max_attempts": 3,
                "must_be_in_stock": True,
                "is_digital_ticket": False,
                "poll_interval_seconds": settings.poll_interval_seconds,
                "deadline_minutes_after_target": settings.deadline_minutes_after_target,
            }
        )
        st.rerun()
with remove_col:
    if len(st.session_state.draft_products) > 1:
        remove_index = st.selectbox(
            "Elimină un rând",
            options=list(range(len(st.session_state.draft_products))),
            format_func=lambda index: f"Produs {index + 1}",
            key="remove_product_index",
        )
        if st.button("Elimină produsul selectat"):
            st.session_state.draft_products.pop(remove_index)
            st.rerun()

with st.form("products_configuration"):
    form_rows: list[dict] = []
    today = datetime.now(ZoneInfo("Europe/Bucharest")).date()
    for index, initial in enumerate(st.session_state.draft_products):
        row_key = initial.get("_key", f"row-{index}")
        st.subheader(f"Produs {index + 1}")
        left, right = st.columns(2)
        with left:
            product_url = st.text_input(
                "Product URL",
                value=initial["product_url"],
                placeholder="https://www.emag.ro/.../pd/CODPRODUS/",
                key=f"product_{row_key}_url",
            )
            max_price = st.text_input(
                "Preț maxim / unitate (RON)",
                value=initial["max_price_ron"],
                key=f"product_{row_key}_max_price",
            )
            quantity = st.number_input(
                "Cantitate dorită",
                min_value=1,
                max_value=999,
                value=int(initial["quantity"]),
                step=1,
                key=f"product_{row_key}_quantity",
            )
            must_be_in_stock = st.checkbox(
                "Trebuie să fie în stoc",
                value=bool(initial["must_be_in_stock"]),
                key=f"product_{row_key}_stock",
            )
            is_digital_ticket = st.checkbox(
                "Este bilet digital (permite fluxul automat de comandă sub 100 RON)",
                value=bool(initial.get("is_digital_ticket", False)),
                key=f"product_{row_key}_digital_ticket",
            )
        with right:
            try:
                initial_date = date.fromisoformat(str(initial["target_date"]))
            except ValueError:
                initial_date = today
            target_date = st.date_input(
                "Data țintă",
                value=initial_date,
                key=f"product_{row_key}_date",
            )
            target_time = st.text_input(
                "Ora țintă (HH:MM:SS, Europe/Bucharest)",
                value=str(initial["target_time"]),
                key=f"product_{row_key}_time",
            )
            max_attempts = st.number_input(
                "Încercări maxime pentru pregătirea coșului (coș comun: o singură încercare)",
                min_value=1,
                max_value=20,
                value=int(initial["max_attempts"]),
                step=1,
                key=f"product_{row_key}_attempts",
            )
            poll_seconds = st.number_input(
                "Interval verificare (secunde, minimum 10)",
                min_value=10,
                max_value=300,
                value=max(10, int(initial["poll_interval_seconds"])),
                step=5,
                key=f"product_{row_key}_poll",
            )
            deadline_minutes = st.number_input(
                "Timp monitorizare după ora țintă (minute)",
                min_value=1,
                max_value=240,
                value=max(1, int(initial["deadline_minutes_after_target"])),
                step=1,
                key=f"product_{row_key}_deadline",
            )
        form_rows.append(
            {
                "product_url": product_url,
                "max_price_ron": max_price,
                "quantity": quantity,
                "target_date": target_date.isoformat(),
                "target_time": target_time,
                "max_attempts": max_attempts,
                "must_be_in_stock": must_be_in_stock,
                "is_digital_ticket": is_digital_ticket,
                "poll_interval_seconds": poll_seconds,
                "deadline_minutes_after_target": deadline_minutes,
            }
        )
    save = st.form_submit_button("Salvează lista de produse", type="primary")

if save:
    try:
        updated = _settings_with_products(
            settings,
            form_rows,
            browser_mode=browser_mode,
            browser_cdp_endpoint=browser_cdp_endpoint,
        )
        save_settings(updated)
        st.session_state.draft_products = [
            _draft_row(product) | {"_key": st.session_state.draft_products[index]["_key"]}
            for index, product in enumerate(updated.products)
        ]
        st.success("Lista a fost salvată în config.yaml.")
        st.rerun()
    except (ConfigurationError, OSError) as exc:
        st.error(str(exc))

test_mode = st.checkbox(
    "Mod test (monitorizează fără să schimbe coșul, checkout-ul sau să trimită comanda)",
    value=True,
    key="test_mode",
)


@st.fragment(run_every="3s")
def live_status() -> None:
    start_col, stop_col, resume_col = st.columns(3)
    with start_col:
        start_clicked = st.button(
            "Pornește monitorizarea", type="primary", disabled=controller.is_running()
        )
    with stop_col:
        stop_clicked = st.button("Oprește", disabled=not controller.is_running())
    with resume_col:
        worker_state = controller.status()
        manual_pending = worker_state.get("state") in {
            "awaiting_manual_login",
            "awaiting_manual_challenge",
        } or any(
            product.get("state") in {"awaiting_manual_login", "awaiting_manual_challenge"}
            for product in worker_state.get("products", [])
        )
        resume_clicked = st.button(
            "Reia după autentificare/verificare manuală",
            disabled=not controller.is_running() or not manual_pending,
        )
    if start_clicked:
        try:
            current_settings = load_settings()
            for product in current_settings.products:
                product.validate()
            run_id = controller.start(
                current_settings.products,
                test_mode=test_mode,
                profile_dir=current_settings.resolved_path(current_settings.browser_profile_dir),
                database_path=current_settings.resolved_path(current_settings.database_path),
                ntp_servers=current_settings.ntp_servers,
                browser_mode=current_settings.browser_mode,
                browser_cdp_endpoint=current_settings.browser_cdp_endpoint,
            )
            st.success(f"Monitorizarea {run_id[:8]} a pornit.")
        except (ConfigurationError, RuntimeError, ValueError, OSError) as exc:
            st.error(str(exc))
    if stop_clicked:
        controller.stop()
    if resume_clicked:
        controller.resume_after_manual_action()

    state = controller.status()
    latest = database.latest_run()
    if state.get("run_id") is None and latest:
        state.update(
            {
                "state": latest["status"],
                "message": latest["status_message"],
                "test_mode": bool(latest["test_mode"]),
                "current_price_ron": latest["current_price_ron"],
                "availability": latest["availability"] or "Unknown",
                "execution_delay_ms": latest["execution_delay_ms"],
                "review": json_value(latest["review_json"]),
            }
        )

    st.subheader("Stare monitorizare")
    st.metric("Stare generală", str(state.get("state", "idle")).replace("_", " ").title())
    st.write(state.get("message", "Pregătit"))
    if state.get("state") == "awaiting_manual_order_review":
        st.warning(
            "Fereastra eMAG rămâne deschisă. Verifică manual produsul, emailul pentru bilete, "
            "taxele și suma. Dacă alegi să continui, apasă tu butonul final în browser; "
            "aplicația nu îl apasă în acest mod de rezervă. Apasă «Oprește» după ce ai terminat."
        )
    if state.get("execution_delay_ms") is not None:
        st.caption(f"Întârziere față de ora țintă: {state['execution_delay_ms']:.0f} ms.")
    product_states = state.get("products", [])
    if product_states:
        st.dataframe(
            [
                {
                    "Produs": product.get("title") or product.get("url"),
                    "Stare": str(product.get("state", "scheduled")).replace("_", " "),
                    "Timp până la țintă": _time_until_target(product.get("target")),
                    "Preț curent (RON)": product.get("current_price_ron") or "—",
                    "Disponibilitate": product.get("availability", "Unknown"),
                    "Întârziere (ms)": product.get("execution_delay_ms"),
                    "Detalii": product.get("message", ""),
                }
                for product in product_states
            ],
            width="stretch",
            hide_index=True,
        )

    review = state.get("review")
    if review:
        st.warning(
            "Rezumatul checkout-ului este disponibil mai jos. Verifică și suma finală afișată de eMAG."
        )
        if review.get("items"):
            st.dataframe(
                [
                    {
                        "Produs": item.get("product_title"),
                        "Cantitate": item.get("quantity"),
                        "Preț unitar (RON)": item.get("unit_price_ron"),
                    }
                    for item in review["items"]
                ],
                width="stretch",
                hide_index=True,
            )
        else:
            st.write(
                {
                    "Produs": review.get("product_title"),
                    "Cantitate": review.get("quantity"),
                    "Preț unitar (RON)": review.get("unit_price_ron"),
                }
            )
        st.write(
            {
                "Subtotal produse (RON)": review.get("product_subtotal_ron"),
                "Total final cu taxe și livrare (RON)": review.get("cart_total_ron"),
                "Metode de plată disponibile": review.get("payment_options", []),
            }
        )

    events = database.recent_events(100)
    st.subheader("Jurnal execuție")
    if events:
        st.dataframe(
            [
                {
                    "Timp (Europe/Bucharest)": event["timestamp_local"],
                    "Nivel": event["level"],
                    "Eveniment": event["event_type"],
                    "Mesaj": event["message"],
                    "Detalii": event["details_json"],
                }
                for event in events
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.caption("Nu există evenimente înregistrate.")


def json_value(value: str | None):
    if not value:
        return None
    import json

    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


live_status()

with st.expander("Profil browser și confidențialitate"):
    st.write(
        (
            "Se conectează la Chrome-ul existent pe endpointul localhost configurat. "
            "Oprește Remote Debugging în Chrome după sesiune."
            if settings.browser_mode == "chrome_cdp"
            else "Autentifică-te manual în browserul vizibil. Sesiunea este păstrată local."
        )
        + " Aplicația nu exportă cookie-uri și nu salvează parole, OTP-uri, numere de card, CVV sau credențiale bancare. "
    )
    if settings.browser_mode == "chrome_cdp":
        st.code(settings.browser_cdp_endpoint)
    else:
        st.code(str(settings.resolved_path(settings.browser_profile_dir)))
