from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright


class BrowserProfileInUse(RuntimeError):
    """The persistent profile is already open in another browser process."""


DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"


def validate_cdp_endpoint(endpoint: str) -> str:
    """Allow only an HTTP CDP endpoint bound to this computer."""
    value = (endpoint or "").strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Chrome CDP endpoint must be a valid localhost URL.") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None
        or not 1 <= port <= 65535
        or parsed.path not in {"", "/"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Chrome CDP endpoint must use localhost, for example http://127.0.0.1:9222."
        )
    return value.rstrip("/")


class BrowserManager:
    """Launch a dedicated profile or attach to a user-authorized local Chrome."""

    def __init__(
        self,
        profile_dir: str | Path,
        *,
        mode: str = "persistent",
        cdp_endpoint: str = DEFAULT_CDP_ENDPOINT,
    ) -> None:
        if mode not in {"persistent", "chrome_cdp"}:
            raise ValueError("Browser mode must be 'persistent' or 'chrome_cdp'.")
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        self.mode = mode
        self.cdp_endpoint = validate_cdp_endpoint(cdp_endpoint) if mode == "chrome_cdp" else cdp_endpoint
        self._playwright: Playwright | None = None
        self._owns_context = False
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def __enter__(self) -> Page:
        self._playwright = sync_playwright().start()
        try:
            if self.mode == "persistent":
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                self.context = self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=False,
                    no_viewport=True,
                    args=["--start-maximized"],
                )
                self._owns_context = True
                self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            else:
                # Fail closed. Never fall back to a fresh, unauthenticated profile.
                browser = self._playwright.chromium.connect_over_cdp(
                    self.cdp_endpoint,
                    timeout=7_000,
                )
                contexts = browser.contexts
                if len(contexts) != 1:
                    raise RuntimeError(
                        "Expected exactly one Chrome profile context on the local CDP connection."
                    )
                self.context = contexts[0]
                # Keep the user's existing tabs intact; automate in a dedicated new tab.
                self.page = self.context.new_page()
        except Exception:
            self._cleanup_after_failed_start()
            raise
        self.page.set_default_timeout(12_000)
        self.page.set_default_navigation_timeout(20_000)
        return self.page

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if self.mode == "chrome_cdp" and self.page is not None:
                # Close only our automation tab. Do not close the user's Chrome context.
                try:
                    if not self.page.is_closed():
                        self.page.close()
                except Exception:
                    pass
            elif self._owns_context and self.context is not None:
                self.context.close()
        finally:
            self.context = None
            self.page = None
            self._owns_context = False
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None

    def _cleanup_after_failed_start(self) -> None:
        try:
            if self.mode == "chrome_cdp" and self.page is not None:
                self.page.close()
            elif self._owns_context and self.context is not None:
                self.context.close()
        except Exception:
            pass
        finally:
            self.page = None
            self.context = None
            self._owns_context = False
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None
