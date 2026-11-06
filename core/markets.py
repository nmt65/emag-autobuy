from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse


@dataclass(frozen=True)
class Market:
    code: str
    host: str
    currency: str
    timezone_name: str
    verified_for_automation: bool


MARKETS: dict[str, Market] = {
    "ro": Market("ro", "emag.ro", "RON", "Europe/Bucharest", True),
    "hu": Market("hu", "emag.hu", "HUF", "Europe/Budapest", False),
}


class MarketUrlError(ValueError):
    """An eMAG URL is not a canonical product URL for a supported market."""


def market_for_url(url: str) -> Market:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise MarketUrlError("Use a normal HTTPS eMAG product link.")
    if host.startswith("www."):
        host = host[4:]
    for market in MARKETS.values():
        if host == market.host:
            return market
    raise MarketUrlError("Use an eMAG Romania or Hungary product link.")


def validate_market_product_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    market = market_for_url(url)
    if not parsed.path or not re.search(r"/pd/[A-Za-z0-9]+/?$", parsed.path, re.IGNORECASE):
        raise MarketUrlError(
            f"The {market.code.upper()} link is not a current product page. "
            "Open the product on that eMAG site and copy its canonical URL containing /pd/ and the product code."
        )
    return parsed.geturl()


_NUMBER_EUR_RON = r"(?:\d{1,3}(?:[.\s]\d{3})+|\d+)(?:\s*,\s*\d{2})?"
_NUMBER_HUF = r"(?:\d{1,3}(?:[.\s]\d{3})+|\d+)"


def parse_market_price(text: str, currency: str) -> Decimal:
    """Parse exactly one amount in the market's native currency; reject ambiguity."""
    patterns = {
        "RON": rf"(?<!\d)({_NUMBER_EUR_RON})\s*(?:lei|ron)\b",
        "EUR": rf"(?<!\d)({_NUMBER_EUR_RON})\s*(?:€(?=\s|$)|eur\b)",
        "HUF": rf"(?<!\d)({_NUMBER_HUF})\s*(?:ft|huf)\b",
    }
    pattern = patterns.get((currency or "").upper())
    if pattern is None:
        raise ValueError("The price currency is not supported.")
    matches = re.findall(pattern, text or "", flags=re.IGNORECASE)
    if len(matches) != 1:
        raise ValueError("The displayed price is missing or ambiguous.")
    normalized = re.sub(r"\s+", "", matches[0])
    if currency.upper() in {"RON", "EUR"}:
        normalized = normalized.replace(".", "").replace(",", ".")
    else:
        normalized = normalized.replace(".", "").replace(",", "")
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("The displayed price could not be parsed safely.") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("The displayed price must be a positive finite amount.")
    return amount
