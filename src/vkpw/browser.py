from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError

from .errors import BrowserNotFoundError


def _windows_candidates() -> list[Path]:
    roots = {
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    }
    relative = (
        Path("Google/Chrome/Application/chrome.exe"),
        Path("Microsoft/Edge/Application/msedge.exe"),
        Path("BraveSoftware/Brave-Browser/Application/brave.exe"),
        Path("Chromium/Application/chrome.exe"),
    )
    return [Path(root) / path for root in roots if root for path in relative]


def _macos_candidates() -> list[Path]:
    return [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    ]


def _unix_candidates() -> list[Path]:
    names = (
        "chromium",
        "chromium-browser",
        "google-chrome-stable",
        "google-chrome",
        "microsoft-edge-stable",
        "microsoft-edge",
        "brave-browser",
        "brave",
    )
    return [Path(found) for name in names if (found := shutil.which(name))]


def find_system_browser(explicit_path: str | Path | None = None) -> Path | None:
    """Найти установленный Chromium, Chrome, Edge или Brave без скачивания браузера."""
    if explicit_path is not None:
        path = Path(explicit_path).expanduser()
        if not path.is_file():
            raise BrowserNotFoundError(f"Файл браузера не найден: {path}")
        return path.resolve()

    from_path = os.environ.get("VKPW_BROWSER_PATH")
    if from_path:
        path = Path(from_path).expanduser()
        if path.is_file():
            return path.resolve()

    candidates: list[Path] = []
    if os.name == "nt":
        candidates.extend(_windows_candidates())
        candidates.extend(_unix_candidates())
    elif sys.platform == "darwin":
        candidates.extend(_macos_candidates())
        candidates.extend(_unix_candidates())
    else:
        candidates.extend(_unix_candidates())

    for path in candidates:
        if path.is_file():
            return path.resolve()
    return None


async def launch_chromium(
    playwright: Any,
    *,
    browser_path: str | Path | None = None,
) -> Any:
    """Запустить браузер в headless-режиме и во временном профиле Playwright."""
    path = find_system_browser(browser_path)
    if path is not None:
        try:
            return await playwright.chromium.launch(
                executable_path=str(path),
                headless=True,
            )
        except PlaywrightError as exc:
            bundled = Path(playwright.chromium.executable_path)
            if browser_path is not None or not bundled.is_file():
                raise BrowserNotFoundError(
                    f"Не удалось запустить найденный браузер {path} ({type(exc).__name__}). "
                    "Попробуйте другой путь через --browser-path или установите Chromium командой "
                    "`vkpw install-browser`."
                ) from exc

    bundled = Path(playwright.chromium.executable_path)
    if bundled.is_file():
        try:
            return await playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            raise BrowserNotFoundError(
                f"Не удалось запустить Chromium Playwright ({type(exc).__name__})."
            ) from exc

    raise BrowserNotFoundError(
        "Не найден Chromium, Chrome, Edge или Brave. Установите один из них либо выполните "
        "`vkpw install-browser` для загрузки Chromium."
    )
