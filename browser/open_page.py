from __future__ import annotations

import argparse
from pathlib import Path

from browser.manager import BrowserManager
from core.markets import market_for_url


def open_manual_page(url: str, profile_dir: str | Path) -> None:
    """Open one user-configured eMAG URL in a visible local Playwright window.

    This helper only navigates to the supplied URL. It does not inspect controls,
    click buttons, modify the basket, or submit an order.
    """
    market_for_url(url)
    with BrowserManager(profile_dir, mode="persistent") as page:
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_event("close", timeout=0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Open an eMAG page for manual local inspection.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--profile-dir", required=True)
    args = parser.parse_args()
    open_manual_page(args.url, args.profile_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
