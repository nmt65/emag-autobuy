from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.markets import (
    MARKETS,
    Market,
    MarketUrlError,
    market_for_url,
    validate_market_product_url,
)


PRICE_PATTERN = re.compile(
    r"(?<!\d)((?:\d{1,3}(?:[.\s]\d{3})+|\d+)(?:\s*,\s*\d{2})?)\s*(?:lei|ron)\b",
    re.IGNORECASE,
)
QUANTITY_WORDS = re.compile(r"cantit|quantity|qty|buc[aă]ți", re.IGNORECASE)
MAX_REQUESTED_QUANTITY = 999
OUT_OF_STOCK_WORDS = (
    "stoc epuizat",
    "indisponibil",
    "momentan nu este disponibil",
    "nu este disponibil",
    "nu e disponibil",
    "nu este in stoc",
    "nu e in stoc",
)
IN_STOCK_WORDS = ("in stoc", "disponibil in stoc")


class ConfigurationError(ValueError):
    """A user configuration value cannot be used safely."""


def parse_romanian_price(text: str) -> Decimal:
    """Parse exactly one visible Lei/RON amount; ambiguous text is rejected."""
    matches = PRICE_PATTERN.findall(text or "")
    if not matches:
        raise ValueError(f"No displayed Lei/RON price found in: {text!r}")
    if len(matches) != 1:
        raise ValueError("More than one displayed price was found; the value is ambiguous.")
    value = matches[0].replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return Decimal(value).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"Could not parse displayed price: {text!r}") from exc


def detect_availability(text: str) -> bool | None:
    """Return True/False when visible text identifies stock, otherwise None."""
    folded = unicodedata.normalize("NFKD", (text or "").casefold())
    without_marks = "".join(char for char in folded if not unicodedata.combining(char))
    normalized = re.sub(r"\s+", " ", without_marks)
    if any(phrase in normalized for phrase in OUT_OF_STOCK_WORDS):
        return False
    if any(phrase in normalized for phrase in IN_STOCK_WORDS) or normalized == "disponibil":
        return True
    return None


def validate_emag_product_url(url: str) -> str:
    try:
        return validate_market_product_url(url)
    except MarketUrlError as exc:
        raise ConfigurationError(str(exc)) from exc


@dataclass(frozen=True)
class ProductConfig:
    product_url: str
    max_price_ron: Decimal
    quantity: int
    target_date: str
    target_time: str
    max_attempts: int
    must_be_in_stock: bool = True
    is_digital_ticket: bool = False
    poll_interval_seconds: int = 30
    deadline_minutes_after_target: int = 15
    timezone_name: str = "Europe/Bucharest"

    @property
    def market(self) -> Market:
        if not self.product_url.strip():
            return MARKETS["ro"]
        return market_for_url(self.product_url)

    @property
    def currency(self) -> str:
        return self.market.currency

    def validate(self) -> None:
        validate_emag_product_url(self.product_url)
        if not self.max_price_ron.is_finite() or self.max_price_ron <= 0:
            raise ConfigurationError(
                f"Maximum price must be greater than 0 {self.currency}."
            )
        if type(self.quantity) is not int or self.quantity < 1 or self.quantity > MAX_REQUESTED_QUANTITY:
            raise ConfigurationError(
                f"Quantity must be between 1 and {MAX_REQUESTED_QUANTITY}."
            )
        if self.max_attempts < 1:
            raise ConfigurationError("Maximum attempts must be at least 1.")
        if self.poll_interval_seconds < 10:
            raise ConfigurationError("Polling interval must be at least 10 seconds.")
        if self.deadline_minutes_after_target < 1:
            raise ConfigurationError("Monitoring deadline must be at least 1 minute.")
        _ = self.target_datetime()

    def target_datetime(self) -> datetime:
        expected_timezone = self.market.timezone_name
        if self.timezone_name != expected_timezone:
            raise ConfigurationError(
                f"Scheduling timezone for eMAG {self.market.code.upper()} must be {expected_timezone}."
            )
        if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", self.target_time):
            raise ConfigurationError("Target time must use HH:MM:SS.")
        try:
            zone = ZoneInfo(self.timezone_name)
            parsed_date = date.fromisoformat(self.target_date)
            parsed_time = time.fromisoformat(self.target_time)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ConfigurationError(
                "Enter a valid target date, HH:MM:SS time, and timezone."
            ) from exc
        naive = datetime.combine(parsed_date, parsed_time)
        candidates = [naive.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
        valid = [
            candidate
            for candidate in candidates
            if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
            == naive
        ]
        if not valid:
            raise ConfigurationError("The selected local time does not exist due to DST.")
        if len(valid) == 2 and valid[0].utcoffset() != valid[1].utcoffset():
            raise ConfigurationError("The selected local time is ambiguous due to DST.")
        return valid[0]


@dataclass(frozen=True)
class ProductObservation:
    title: str
    price_ron: Decimal | None
    in_stock: bool | None
    can_purchase: bool
    page_url: str
    observed_at: datetime
    currency: str = "RON"


@dataclass(frozen=True)
class ConditionResult:
    ready: bool
    reasons: tuple[str, ...]


def evaluate_product_conditions(
    observation: ProductObservation, config: ProductConfig
) -> ConditionResult:
    reasons: list[str] = []
    if observation.currency.upper() != config.currency:
        reasons.append(
            f"Observed currency {observation.currency.upper()} does not match "
            f"the configured market currency {config.currency}."
        )
    if observation.price_ron is None:
        reasons.append("Displayed price could not be read.")
    elif observation.price_ron > config.max_price_ron:
        reasons.append(
            f"Price {observation.price_ron:.2f} {config.currency} exceeds the maximum "
            f"{config.max_price_ron:.2f} {config.currency}."
        )
    if observation.in_stock is None:
        reasons.append("Product availability could not be verified.")
    elif config.must_be_in_stock and observation.in_stock is not True:
        reasons.append("Product is not confirmed in stock.")
    if not observation.can_purchase:
        reasons.append("The visible add-to-cart control is unavailable.")
    return ConditionResult(ready=not reasons, reasons=tuple(reasons))


@dataclass(frozen=True)
class QuantityControlCandidate:
    control_type: str
    name: str = ""
    label: str = ""
    value: str = ""
    row_text: str = ""
    visible: bool = True
    minimum: int | None = None
    maximum: int | None = None
    options: tuple[str, ...] = ()


def choose_quantity_control(
    candidates: Iterable[QuantityControlCandidate], product_title: str
) -> int | None:
    """Choose a quantity field only when its purpose and product are unambiguous."""
    controls = [item for item in candidates if item.visible]
    likely = [
        (index, item)
        for index, item in enumerate(controls)
        if item.control_type.casefold() in {"number", "select"}
        or QUANTITY_WORDS.search(f"{item.name} {item.label}")
    ]
    if not likely:
        return None
    title = " ".join(product_title.casefold().split())
    associated = [
        index
        for index, (_, item) in enumerate(likely)
        if title and title in " ".join(item.row_text.casefold().split())
    ]
    if len(associated) == 1:
        return likely[associated[0]][0]
    return None


def is_quantity_control_candidate(candidate: QuantityControlCandidate) -> bool:
    return bool(
        candidate.visible
        and (
            candidate.control_type.casefold() in {"number", "select"}
            or QUANTITY_WORDS.search(f"{candidate.name} {candidate.label}")
        )
    )


def quantity_within_limits(
    requested: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    options: Iterable[str] = (),
) -> bool:
    if type(requested) is not int or requested < 1 or requested > MAX_REQUESTED_QUANTITY:
        return False
    if requested < (minimum if minimum is not None else 1):
        return False
    if maximum is not None and requested > maximum:
        return False
    allowed_options = {str(option) for option in options}
    return not allowed_options or str(requested) in allowed_options


@dataclass(frozen=True)
class CartVerification:
    verified: bool
    reasons: tuple[str, ...]


def verify_cart(
    *,
    visible_cart_text: str,
    expected_title: str,
    requested_quantity: int,
    observed_quantity: int | None,
    unit_price_ron: Decimal,
    cart_total_ron: Decimal | None,
    max_price_ron: Decimal,
    expected_unit_price_ron: Decimal | None = None,
    product_subtotal_ron: Decimal | None = None,
) -> CartVerification:
    reasons: list[str] = []
    normalized_cart = " ".join((visible_cart_text or "").casefold().split())
    normalized_title = " ".join((expected_title or "").casefold().split())
    if not normalized_title or normalized_title not in normalized_cart:
        reasons.append("The expected product is not visible in the cart.")
    if observed_quantity != requested_quantity:
        reasons.append(
            f"Cart quantity is {observed_quantity!r}; expected {requested_quantity}."
        )
    if unit_price_ron > max_price_ron:
        reasons.append("The cart unit price is above the configured maximum.")
    if expected_unit_price_ron is not None and unit_price_ron != expected_unit_price_ron:
        reasons.append("The cart unit price changed from the verified product page price.")
    expected_subtotal = unit_price_ron * requested_quantity
    if product_subtotal_ron is None:
        reasons.append("The cart product subtotal could not be verified.")
    elif product_subtotal_ron != expected_subtotal:
        reasons.append(
            f"The product subtotal is {product_subtotal_ron:.2f} RON; "
            f"expected {expected_subtotal:.2f} RON."
        )
    if cart_total_ron is None:
        reasons.append("The visible cart total could not be verified.")
    elif cart_total_ron < unit_price_ron * requested_quantity:
        reasons.append("The cart total is lower than the expected product subtotal.")
    return CartVerification(verified=not reasons, reasons=tuple(reasons))


def parse_cart_total(text: str) -> Decimal | None:
    """Return a total only if its visible fee breakdown is complete and reconciles."""
    breakdown = parse_order_total_breakdown(text)
    return breakdown.total_ron if breakdown.complete else None


@dataclass(frozen=True)
class OrderTotalBreakdown:
    total_ron: Decimal | None
    product_subtotal_ron: Decimal | None
    complete: bool
    reasons: tuple[str, ...]


def parse_order_total_breakdown(text: str) -> OrderTotalBreakdown:
    """Reconcile the visible product, delivery, fee, discount, and payable lines."""
    total_labels = re.compile(
        r"^(?:total|total de plat[aă]|total de achitat|total comand[aă]|totalul comenzii)\b",
        re.IGNORECASE,
    )
    product_labels = re.compile(
        r"^(?:cost produse|subtotal produse|subtotal|valoare produse)\b",
        re.IGNORECASE,
    )
    delivery_labels = re.compile(
        r"^(?:cost livrare|tax[aă] livrare|livrare|transport|shipping)\b",
        re.IGNORECASE,
    )
    fee_labels = re.compile(
        r"(?:tax[aă]|comision|ambalaj|servici(?:u|i)|ramburs|asigurare|fee)",
        re.IGNORECASE,
    )
    discount_labels = re.compile(r"(?:reducere|discount|voucher|econom)", re.IGNORECASE)

    totals: list[Decimal] = []
    products: list[Decimal] = []
    deliveries: list[Decimal] = []
    fees: list[Decimal] = []
    discounts: list[Decimal] = []
    unknown_amount_lines: list[str] = []
    reasons: list[str] = []

    # Dynamic eMAG markup may split a displayed amount over adjacent inline
    # text nodes. Rejoin continuation lines only within an explicitly labeled
    # charge row; never infer which unlabeled value belongs to which fee.
    merged_lines: list[str] = []
    current_line = ""
    for raw_line in (text or "").splitlines():
        part = " ".join(raw_line.split()).strip()
        if not part:
            continue
        if ":" in part:
            if current_line:
                merged_lines.append(current_line)
            current_line = part
        elif current_line:
            current_line = f"{current_line} {part}"
    if current_line:
        merged_lines.append(current_line)

    for raw_line in merged_lines:
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        label = line.split(":", 1)[0].strip()
        has_amount = bool(
            PRICE_PATTERN.search(line)
            or re.search(r"\bgratuit\b", line, re.I)
            or (":" in line and re.search(r"\d", line))
        )
        if not has_amount:
            continue
        try:
            amount = Decimal("0.00") if re.search(r"\bgratuit\b", line, re.I) else parse_romanian_price(line)
        except (ValueError, InvalidOperation):
            unknown_amount_lines.append(line)
            continue

        if total_labels.match(label):
            totals.append(amount)
        elif product_labels.match(label):
            products.append(amount)
        elif delivery_labels.match(label):
            deliveries.append(amount)
        elif discount_labels.search(label):
            discounts.append(amount)
        elif fee_labels.search(label):
            fees.append(amount)
        else:
            unknown_amount_lines.append(line)

    if len(totals) != 1:
        reasons.append("A single final payable total is not visible.")
    if len(products) != 1:
        reasons.append("Exactly one product subtotal is not visible.")
    if not deliveries:
        reasons.append("The delivery charge or free-delivery status is not visible.")
    if unknown_amount_lines:
        reasons.append("One or more displayed amounts have an unclassified charge label.")
    total = totals[0] if len(totals) == 1 else None
    if total is not None and products and deliveries and not unknown_amount_lines:
        reconciled = sum(products, Decimal("0.00")) + sum(deliveries, Decimal("0.00"))
        reconciled += sum(fees, Decimal("0.00"))
        reconciled -= sum(discounts, Decimal("0.00"))
        if reconciled != total:
            reasons.append(
                f"Visible fees and discounts sum to {reconciled:.2f} RON, "
                f"but the payable total is {total:.2f} RON."
            )
    return OrderTotalBreakdown(
        total_ron=total,
        product_subtotal_ron=products[0] if len(products) == 1 else None,
        complete=not reasons,
        reasons=tuple(reasons),
    )
