"""Прохождение проверки VK «Я не робот» (robot check) в браузере.

Проверка VK ID выглядит как модальное окно с заголовком
«Подтвердите, что вы не робот» и чекбоксом «Я не робот». Модуль находит это
окно в основном документе или во фрейме, кликает по чекбоксу и проверяет, что
проверка исчезла. Если вместо чекбокса VK показывает картинку или код, клик не
помогает — вызывающий код решает, что нужно ручное подтверждение.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

ROBOT_CHECK_MARKERS = (
    "я не робот",
    "подтвердите, что вы не робот",
    "not a robot",
    "confirm you are not a robot",
    "confirm you're not a robot",
)

_ROBOT_CHECK_LABEL = re.compile(r"(?:я\s+не\s+робот|not\s+a\s+robot)", re.IGNORECASE)
_CAPTCHA_FRAME_MARKERS = ("captcha", "robot-check", "robotcheck")
_CHECKBOX_SELECTORS = (
    '[role="checkbox"]',
    'input[type="checkbox"]',
    '[data-testid*="checkbox" i]',
    '[class*="checkbox" i]',
)
_LABELED_CONTROL_SELECTORS = (
    '[aria-label*="робот" i]',
    '[aria-label*="robot" i]',
    '[title*="робот" i]',
    '[title*="robot" i]',
)
_CHECKBOX_OFFSET = 26.0
_LABEL_LIMIT = 5
_SETTLE_SECONDS = 8.0
_RESOLVE_TIMEOUT = 30.0
_POLL_INTERVAL = 0.25


def _page_frames(page: Any) -> list[Any]:
    """Список фреймов страницы; при недоступном `frames` — сама страница."""
    try:
        frames = list(page.frames)
    except Exception:  # noqa: BLE001 - страница может быть подменена в тестах
        frames = []
    return frames or [page]


async def _frame_text(frame: Any) -> str:
    try:
        return (await frame.locator("body").inner_text(timeout=1500)).casefold()
    except Exception:  # noqa: BLE001 - фрейм может быть недоступен или пуст
        return ""


def _is_robot_check_text(text: str) -> bool:
    return any(marker in text for marker in ROBOT_CHECK_MARKERS)


async def _is_visible(element: Any) -> bool:
    try:
        return bool(await element.is_visible())
    except Exception:  # noqa: BLE001 - элемент может исчезнуть между проверками
        return False


async def _safe_bounding_box(element: Any) -> dict[str, float] | None:
    try:
        box = await element.bounding_box()
    except Exception:  # noqa: BLE001 - элемент может не иметь геометрии
        return None
    if not box:
        return None
    return {
        "x": float(box["x"]),
        "y": float(box["y"]),
        "width": float(box["width"]),
        "height": float(box["height"]),
    }


async def _click_element(element: Any) -> bool:
    try:
        await element.click(timeout=3_000)
    except Exception:  # noqa: BLE001 - клик может не попасть в элемент
        return False
    return True


async def _locator_elements(frame: Any, selector: str, limit: int) -> list[Any]:
    try:
        locator = frame.locator(selector)
        count = await locator.count()
    except Exception:  # noqa: BLE001 - фрейм может быть недоступен
        return []
    elements: list[Any] = []
    for index in range(min(count, limit)):
        elements.append(locator.nth(index))
    return elements


async def _has_visible_checkbox(frame: Any) -> bool:
    for selector in _CHECKBOX_SELECTORS:
        for element in await _locator_elements(frame, selector, 5):
            if await _is_visible(element):
                return True
    return False


async def _has_labeled_control(frame: Any) -> bool:
    """Есть ли элемент с подписью «Я не робот» в aria-label или title."""
    for selector in _LABELED_CONTROL_SELECTORS:
        for element in await _locator_elements(frame, selector, 5):
            if await _is_visible(element):
                return True
    return False


async def find_robot_check(page: Any) -> Any | None:
    """Найти фрейм, в котором показан чекбокс «Я не робот»."""
    frames = _page_frames(page)
    for frame in frames:
        if _is_robot_check_text(await _frame_text(frame)):
            return frame
    for frame in frames:
        if await _has_labeled_control(frame):
            return frame
    main_frame = getattr(page, "main_frame", None)
    for frame in frames:
        if frame is main_frame:
            continue
        url = str(getattr(frame, "url", "") or "").casefold()
        if any(marker in url for marker in _CAPTCHA_FRAME_MARKERS) and await _has_visible_checkbox(
            frame
        ):
            return frame
    return None


async def has_robot_check(page: Any) -> bool:
    """Показана ли сейчас проверка «Я не робот»."""
    return await find_robot_check(page) is not None


async def _find_label(frame: Any) -> Any | None:
    """Найти наименьший видимый элемент с подписью «Я не робот»."""
    try:
        matches = frame.get_by_text(_ROBOT_CHECK_LABEL)
        count = await matches.count()
    except Exception:  # noqa: BLE001 - фрейм может быть недоступен
        return None
    best: Any | None = None
    best_area: float | None = None
    for index in range(min(count, _LABEL_LIMIT)):
        candidate = matches.nth(index)
        if not await _is_visible(candidate):
            continue
        box = await _safe_bounding_box(candidate)
        if box is None:
            return candidate
        area = box["width"] * box["height"]
        if best_area is None or area < best_area:
            best = candidate
            best_area = area
    return best


def _is_beside_label(box: dict[str, float], label_box: dict[str, float]) -> bool:
    """Чекбокс расположен слева от подписи на той же строке."""
    if box["y"] + box["height"] < label_box["y"] - 4:
        return False
    if box["y"] > label_box["y"] + label_box["height"] + 4:
        return False
    if box["x"] < label_box["x"] + label_box["width"] and box["x"] + box["width"] > label_box["x"]:
        return True
    return -12.0 <= label_box["x"] - (box["x"] + box["width"]) <= 120.0


async def _click_dom_checkbox(frame: Any, label_box: dict[str, float] | None) -> bool:
    """Кликнуть по настоящему чекбоксу рядом с подписью."""
    for selector in (*_LABELED_CONTROL_SELECTORS, *_CHECKBOX_SELECTORS):
        for candidate in await _locator_elements(frame, selector, 10):
            if not await _is_visible(candidate):
                continue
            box = await _safe_bounding_box(candidate)
            if label_box is not None and box is not None and not _is_beside_label(
                box, label_box
            ):
                continue
            if await _click_element(candidate):
                return True
    return False


async def _click_offset(page: Any, label_box: dict[str, float]) -> bool:
    """Кликнуть по квадрату чекбокса слева от подписи."""
    x = label_box["x"] - _CHECKBOX_OFFSET
    y = label_box["y"] + label_box["height"] / 2
    try:
        await page.mouse.move(x, y, steps=10)
        await asyncio.sleep(0.15)
        await page.mouse.click(x, y)
    except Exception:  # noqa: BLE001 - мышь может быть недоступна
        return False
    return True


async def _wait_cleared(page: Any, *, deadline: float) -> bool:
    """Ждать, пока окно проверки исчезнет."""
    settle_deadline = min(deadline, time.monotonic() + _SETTLE_SECONDS)
    while time.monotonic() < settle_deadline:
        if not await has_robot_check(page):
            return True
        await asyncio.sleep(_POLL_INTERVAL)
    return not await has_robot_check(page)


async def solve_robot_check(page: Any, *, timeout: float = _RESOLVE_TIMEOUT) -> bool:
    """Пройти проверку «Я не робот» кликом по чекбоксу.

    Возвращает `True`, если окно проверки исчезло. Если клик не помог или VK
    показал другую проверку, возвращается `False` — вызывающий код переходит
    к ручному подтверждению.
    """
    frame = await find_robot_check(page)
    if frame is None:
        return False

    deadline = time.monotonic() + timeout
    label = await _find_label(frame)
    label_box = await _safe_bounding_box(label) if label is not None else None

    if await _click_dom_checkbox(frame, label_box):
        logger.info("Проверка «Я не робот»: выполнен клик по чекбоксу")
    elif label_box is not None and await _click_offset(page, label_box):
        logger.info("Проверка «Я не робот»: выполнен клик по квадрату рядом с подписью")
    elif label is not None and await _click_element(label):
        logger.info("Проверка «Я не робот»: выполнен клик по подписи чекбокса")
    else:
        logger.info("Проверка «Я не робот»: чекбокс не найден")
        return False

    if await _wait_cleared(page, deadline=deadline):
        logger.info("Проверка «Я не робот» пройдена")
        return True

    logger.info("Проверка «Я не робот» после клика не исчезла")
    return False
