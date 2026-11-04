from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vkpw.browser import find_system_browser, launch_chromium
from vkpw.errors import BrowserNotFoundError


class FakeChromium:
    executable_path = "unused-managed-browser"

    def __init__(self) -> None:
        self.launch_options = None

    async def launch(self, **options):
        self.launch_options = options
        return options


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_launch_prefers_explicit_existing_browser_headlessly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            browser_path = Path(directory) / "chrome"
            browser_path.write_text("browser", encoding="utf-8")
            playwright = FakePlaywright()

            result = await launch_chromium(playwright, browser_path=browser_path)

            self.assertEqual(result["executable_path"], str(browser_path.resolve()))
            self.assertTrue(result["headless"])
            self.assertNotIn("user_data_dir", result)

    async def test_launch_uses_bundled_browser_headlessly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundled_path = Path(directory) / "playwright-chromium"
            bundled_path.touch()
            playwright = FakePlaywright()
            playwright.chromium.executable_path = str(bundled_path)

            with patch("vkpw.browser.find_system_browser", return_value=None):
                result = await launch_chromium(playwright)

            self.assertEqual(result, {"headless": True})

    async def test_missing_explicit_browser_is_reported(self) -> None:
        with self.assertRaises(BrowserNotFoundError):
            await launch_chromium(FakePlaywright(), browser_path="missing-browser")

    def test_explicit_browser_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            browser_path = Path(directory) / "chrome"
            browser_path.touch()
            self.assertEqual(find_system_browser(browser_path), browser_path.resolve())

    def test_missing_system_browser_returns_none(self) -> None:
        with (
            patch("vkpw.browser._windows_candidates", return_value=[]),
            patch("vkpw.browser._macos_candidates", return_value=[]),
            patch("vkpw.browser._unix_candidates", return_value=[]),
        ):
            self.assertIsNone(find_system_browser())


if __name__ == "__main__":
    unittest.main()
