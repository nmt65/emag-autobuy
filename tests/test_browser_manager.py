import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from browser.manager import BrowserManager, validate_cdp_endpoint


class FakePage:
    def __init__(self):
        self.closed = False
        self.default_timeout = None
        self.navigation_timeout = None

    def set_default_timeout(self, value):
        self.default_timeout = value

    def set_default_navigation_timeout(self, value):
        self.navigation_timeout = value

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.existing_page = FakePage()
        self.pages = [self.existing_page]
        self.created_page = None
        self.close_calls = 0

    def new_page(self):
        self.created_page = FakePage()
        self.pages.append(self.created_page)
        return self.created_page

    def close(self):
        self.close_calls += 1


class FakeBrowser:
    def __init__(self, contexts=None):
        self.contexts = contexts if contexts is not None else [FakeContext()]


class FakeChromium:
    def __init__(self, browser=None, connect_error=None):
        self.browser = browser or FakeBrowser()
        self.connect_error = connect_error
        self.connect_calls = []
        self.launch_calls = []

    def connect_over_cdp(self, endpoint, *, timeout):
        self.connect_calls.append((endpoint, timeout))
        if self.connect_error:
            raise self.connect_error
        return self.browser

    def launch_persistent_context(self, **kwargs):
        self.launch_calls.append(kwargs)
        return FakeContext()


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1


class FakePlaywrightFactory:
    def __init__(self, playwright):
        self.playwright = playwright

    def start(self):
        return self.playwright


class BrowserManagerTests(unittest.TestCase):
    def test_cdp_endpoint_requires_loopback_http(self):
        self.assertEqual(
            validate_cdp_endpoint("http://127.0.0.1:9222/"),
            "http://127.0.0.1:9222",
        )
        for endpoint in (
            "https://127.0.0.1:9222",
            "http://192.168.1.10:9222",
            "http://localhost:9222/path",
            "http://user:secret@127.0.0.1:9222",
            "http://127.0.0.1:0",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validate_cdp_endpoint(endpoint)

    def test_cdp_uses_new_tab_and_leaves_existing_chrome_open(self):
        context = FakeContext()
        browser = FakeBrowser([context])
        chromium = FakeChromium(browser)
        playwright = FakePlaywright(chromium)
        factory = FakePlaywrightFactory(playwright)
        with tempfile.TemporaryDirectory() as directory, patch(
            "browser.manager.sync_playwright", return_value=factory
        ):
            manager = BrowserManager(
                Path(directory) / "unused-profile",
                mode="chrome_cdp",
                cdp_endpoint="http://127.0.0.1:9222",
            )
            with manager as page:
                self.assertIs(page, context.created_page)
                self.assertIsNot(page, context.existing_page)
                self.assertEqual(page.default_timeout, 12_000)
                self.assertEqual(page.navigation_timeout, 20_000)

        self.assertTrue(context.created_page.closed)
        self.assertFalse(context.existing_page.closed)
        self.assertEqual(context.close_calls, 0)
        self.assertEqual(chromium.launch_calls, [])
        self.assertEqual(chromium.connect_calls, [("http://127.0.0.1:9222", 7_000)])
        self.assertEqual(playwright.stop_calls, 1)

    def test_failed_cdp_connection_does_not_fall_back_to_new_profile(self):
        chromium = FakeChromium(connect_error=RuntimeError("no CDP listener"))
        playwright = FakePlaywright(chromium)
        factory = FakePlaywrightFactory(playwright)
        with tempfile.TemporaryDirectory() as directory, patch(
            "browser.manager.sync_playwright", return_value=factory
        ):
            manager = BrowserManager(
                Path(directory) / "unused-profile",
                mode="chrome_cdp",
                cdp_endpoint="http://127.0.0.1:9222",
            )
            with self.assertRaisesRegex(RuntimeError, "no CDP listener"):
                manager.__enter__()

        self.assertEqual(chromium.launch_calls, [])
        self.assertEqual(playwright.stop_calls, 1)

    def test_multiple_cdp_contexts_fail_closed(self):
        chromium = FakeChromium(FakeBrowser([FakeContext(), FakeContext()]))
        playwright = FakePlaywright(chromium)
        factory = FakePlaywrightFactory(playwright)
        with tempfile.TemporaryDirectory() as directory, patch(
            "browser.manager.sync_playwright", return_value=factory
        ):
            manager = BrowserManager(
                Path(directory) / "unused-profile",
                mode="chrome_cdp",
                cdp_endpoint="http://127.0.0.1:9222",
            )
            with self.assertRaisesRegex(RuntimeError, "exactly one Chrome profile context"):
                manager.__enter__()

        self.assertEqual(chromium.launch_calls, [])
        self.assertEqual(playwright.stop_calls, 1)


if __name__ == "__main__":
    unittest.main()
