from __future__ import annotations

import unittest
from unittest.mock import patch

from vkpw.challenge import find_robot_check, has_robot_check, solve_robot_check

LABEL_BOX = {"x": 614.0, "y": 415.0, "width": 90.0, "height": 20.0}
CHECKBOX_BOX = {"x": 580.0, "y": 414.0, "width": 16.0, "height": 16.0}
ROBOT_TEXT = "Подтвердите, что вы не робот\nЯ не робот"


class _FakeElement:
    def __init__(self, *, visible: bool = True, box=None, on_click=None) -> None:
        self.visible = visible
        self.box = box
        self.on_click = on_click
        self.clicks = 0

    async def is_visible(self) -> bool:
        return self.visible

    async def bounding_box(self):
        return self.box

    async def click(self, timeout: int | None = None) -> None:
        self.clicks += 1
        if self.on_click is not None:
            self.on_click()


class _FakeLocator:
    def __init__(self, elements) -> None:
        self.elements = list(elements)

    async def count(self) -> int:
        return len(self.elements)

    def nth(self, index: int) -> _FakeElement:
        return self.elements[index]


class _FakeBodyLocator:
    def __init__(self, frame) -> None:
        self.frame = frame

    async def inner_text(self, timeout: int | None = None) -> str:
        return self.frame.text


class _FakeFrame:
    def __init__(
        self,
        *,
        text: str = "",
        url: str = "https://id.vk.ru/auth",
        checkboxes: list[_FakeElement] | None = None,
        labels: list[_FakeElement] | None = None,
        labeled: list[_FakeElement] | None = None,
    ) -> None:
        self.text = text
        self.url = url
        self.checkboxes = list(checkboxes or [])
        self.labels = list(labels or [])
        self.labeled = list(labeled or [])

    def locator(self, selector: str):
        if selector == "body":
            return _FakeBodyLocator(self)
        if "aria-label" in selector or "title" in selector:
            return _FakeLocator(self.labeled)
        return _FakeLocator(self.checkboxes)

    def get_by_text(self, pattern):
        return _FakeLocator(self.labels)


class _FakeMouse:
    def __init__(self) -> None:
        self.moves: list[tuple[float, float]] = []
        self.clicks: list[tuple[float, float]] = []

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        self.moves.append((x, y))

    async def click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))


class _FakePage:
    def __init__(self, frames: list[_FakeFrame]) -> None:
        self.frames = frames
        self.main_frame = frames[0] if frames else None
        self.mouse = _FakeMouse()

    def locator(self, selector: str):
        return _FakeLocator([])

    def get_by_text(self, pattern):
        return _FakeLocator([])


def _robot_frame(*, checkbox: _FakeElement | None = None) -> _FakeFrame:
    frame = _FakeFrame(text=ROBOT_TEXT)
    if checkbox is not None:
        frame.checkboxes = [checkbox]
    return frame


class RobotCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_robot_check_is_detected_by_page_text(self) -> None:
        frame = _robot_frame()
        page = _FakePage([frame])

        self.assertTrue(await has_robot_check(page))
        self.assertIs(await find_robot_check(page), frame)

    async def test_control_labeled_as_robot_is_detected_and_clicked(self) -> None:
        frame = _FakeFrame(text="")
        control = _FakeElement(box=CHECKBOX_BOX)
        frame.labeled = [control]

        def clear_check() -> None:
            frame.labeled = []
            frame.text = ""

        control.on_click = clear_check
        page = _FakePage([frame])

        self.assertTrue(await has_robot_check(page))

        with patch("vkpw.challenge._SETTLE_SECONDS", 0.2):
            solved = await solve_robot_check(page)

        self.assertTrue(solved)
        self.assertEqual(control.clicks, 1)

    async def test_robot_check_is_detected_inside_frame(self) -> None:
        main = _FakeFrame(text="Вход в аккаунт")
        inner = _robot_frame()
        inner.url = "https://captcha.vk.ru/robot-check"
        page = _FakePage([main, inner])

        self.assertIs(await find_robot_check(page), inner)

    async def test_page_without_robot_check_is_not_detected(self) -> None:
        page = _FakePage([_FakeFrame(text="Вход в аккаунт")])

        self.assertFalse(await has_robot_check(page))
        self.assertIsNone(await find_robot_check(page))

    async def test_checkbox_next_to_label_is_clicked(self) -> None:
        frame = _robot_frame()
        label = _FakeElement(box=LABEL_BOX)
        frame.labels = [label]

        def clear_check() -> None:
            frame.text = ""

        checkbox = _FakeElement(box=CHECKBOX_BOX, on_click=clear_check)
        frame.checkboxes = [checkbox]
        page = _FakePage([frame])

        with patch("vkpw.challenge._SETTLE_SECONDS", 0.2):
            solved = await solve_robot_check(page)

        self.assertTrue(solved)
        self.assertEqual(checkbox.clicks, 1)
        self.assertEqual(page.mouse.clicks, [])

    async def test_unrelated_checkbox_is_not_clicked(self) -> None:
        frame = _robot_frame()
        label = _FakeElement(box=LABEL_BOX)
        frame.labels = [label]
        unrelated = _FakeElement(box={"x": 100.0, "y": 500.0, "width": 16.0, "height": 16.0})
        frame.checkboxes = [unrelated]
        page = _FakePage([frame])

        with patch("vkpw.challenge._SETTLE_SECONDS", 0.2):
            solved = await solve_robot_check(page)

        self.assertFalse(solved)
        self.assertEqual(unrelated.clicks, 0)
        self.assertEqual(page.mouse.clicks, [(LABEL_BOX["x"] - 26.0, LABEL_BOX["y"] + 10.0)])

    async def test_click_next_to_label_is_used_without_dom_checkbox(self) -> None:
        frame = _robot_frame()
        label = _FakeElement(box=LABEL_BOX)
        frame.labels = [label]
        page = _FakePage([frame])

        def clear_check() -> None:
            frame.text = ""

        page.mouse.clicks.clear()
        original_click = page.mouse.click

        async def click(x: float, y: float) -> None:
            await original_click(x, y)
            clear_check()

        page.mouse.click = click  # type: ignore[method-assign]

        with patch("vkpw.challenge._SETTLE_SECONDS", 0.2):
            solved = await solve_robot_check(page)

        self.assertTrue(solved)
        self.assertEqual(len(page.mouse.clicks), 1)

    async def test_missing_checkbox_keeps_challenge_open(self) -> None:
        page = _FakePage([_robot_frame()])

        with patch("vkpw.challenge._SETTLE_SECONDS", 0.2):
            solved = await solve_robot_check(page)

        self.assertFalse(solved)
        self.assertEqual(page.mouse.clicks, [])

    async def test_challenge_that_survives_click_is_not_reported_as_solved(self) -> None:
        frame = _robot_frame()
        checkbox = _FakeElement(box=CHECKBOX_BOX)
        frame.checkboxes = [checkbox]
        page = _FakePage([frame])

        with patch("vkpw.challenge._SETTLE_SECONDS", 0.2):
            solved = await solve_robot_check(page)

        self.assertFalse(solved)
        self.assertEqual(checkbox.clicks, 1)


if __name__ == "__main__":
    unittest.main()
