from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from core.conditions import ProductConfig
from core.markets import MARKETS, market_for_url


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.yaml"


@dataclass(frozen=True)
class AppSettings:
    product: ProductConfig
    additional_products: tuple[ProductConfig, ...] = ()
    timezone_name: str = "Europe/Bucharest"
    poll_interval_seconds: int = 30
    deadline_minutes_after_target: int = 15
    ntp_servers: tuple[str, ...] = ("time.cloudflare.com", "pool.ntp.org")
    browser_profile_dir: str = "data/browser-profile"
    browser_mode: str = "persistent"
    browser_cdp_endpoint: str = "http://127.0.0.1:9222"
    database_path: str = "data/emag_autobuy.sqlite3"

    @property
    def products(self) -> tuple[ProductConfig, ...]:
        return (self.product, *self.additional_products)

    def resolved_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (PROJECT_DIR / path).resolve()

    def to_mapping(self) -> dict[str, Any]:
        products = []
        for configured_product in self.products:
            product = asdict(configured_product)
            product.pop("max_price_ron", None)
            product["max_price_native"] = float(configured_product.max_price_ron)
            product["currency"] = configured_product.currency
            product["market"] = configured_product.market.code
            products.append(product)
        return {
            "timezone": self.timezone_name,
            "poll_interval_seconds": self.poll_interval_seconds,
            "deadline_minutes_after_target": self.deadline_minutes_after_target,
            "ntp_servers": list(self.ntp_servers),
            "browser_profile_dir": self.browser_profile_dir,
            "browser_mode": self.browser_mode,
            "browser_cdp_endpoint": self.browser_cdp_endpoint,
            "database_path": self.database_path,
            "products": products,
        }


def load_settings(path: str | Path = DEFAULT_CONFIG_PATH) -> AppSettings:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = (PROJECT_DIR / config_path).resolve()
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.safe_dump(
                {
                    "timezone": "Europe/Bucharest",
                    "poll_interval_seconds": 30,
                    "deadline_minutes_after_target": 15,
                    "ntp_servers": ["time.cloudflare.com", "pool.ntp.org"],
                    "browser_profile_dir": "data/browser-profile",
                    "browser_mode": "persistent",
                    "browser_cdp_endpoint": "http://127.0.0.1:9222",
                    "database_path": "data/emag_autobuy.sqlite3",
                    "product": {},
                },
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    product_entries = raw.get("products")
    if not isinstance(product_entries, list) or not product_entries:
        product_entries = [raw.get("product") or {}]

    products: list[ProductConfig] = []
    for product_raw in product_entries:
        if not isinstance(product_raw, dict):
            raise ValueError("Each configured product must be a YAML mapping.")
        product_url = str(product_raw.get("product_url", ""))
        market = market_for_url(product_url) if product_url.strip() else MARKETS["ro"]
        configured_currency = str(product_raw.get("currency", market.currency)).upper()
        configured_market = str(product_raw.get("market", market.code)).lower()
        if configured_market != market.code:
            raise ValueError(
                f"Configured market {configured_market.upper()} does not match "
                f"the eMAG URL market {market.code.upper()}."
            )
        if configured_currency != market.currency:
            raise ValueError(
                f"Configured currency {configured_currency} does not match "
                f"eMAG {market.code.upper()} currency {market.currency}."
            )
        products.append(
            ProductConfig(
                product_url=product_url,
                max_price_ron=Decimal(
                    str(product_raw.get("max_price_native", product_raw.get("max_price_ron", 0)))
                ),
                quantity=int(product_raw.get("quantity", 1)),
                target_date=str(product_raw.get("target_date", "")),
                target_time=str(product_raw.get("target_time", "12:00:00")),
                max_attempts=int(product_raw.get("max_attempts", 3)),
                must_be_in_stock=bool(product_raw.get("must_be_in_stock", True)),
                is_digital_ticket=bool(product_raw.get("is_digital_ticket", False)),
                poll_interval_seconds=int(
                    product_raw.get(
                        "poll_interval_seconds", raw.get("poll_interval_seconds", 30)
                    )
                ),
                deadline_minutes_after_target=int(
                    product_raw.get(
                        "deadline_minutes_after_target",
                        raw.get("deadline_minutes_after_target", 15),
                    )
                ),
                timezone_name=str(
                    product_raw.get("timezone_name", market.timezone_name)
                ),
            )
        )
    product = products[0]
    servers = raw.get("ntp_servers") or ["time.cloudflare.com", "pool.ntp.org"]
    return AppSettings(
        product=product,
        additional_products=tuple(products[1:]),
        timezone_name=product.timezone_name,
        poll_interval_seconds=product.poll_interval_seconds,
        deadline_minutes_after_target=product.deadline_minutes_after_target,
        ntp_servers=tuple(str(server) for server in servers),
        browser_profile_dir=str(raw.get("browser_profile_dir", "data/browser-profile")),
        browser_mode=str(raw.get("browser_mode", "persistent")),
        browser_cdp_endpoint=str(
            raw.get("browser_cdp_endpoint", "http://127.0.0.1:9222")
        ),
        database_path=str(raw.get("database_path", "data/emag_autobuy.sqlite3")),
    )


def save_settings(
    settings: AppSettings, path: str | Path = DEFAULT_CONFIG_PATH
) -> None:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = (PROJECT_DIR / config_path).resolve()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(settings.to_mapping(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
